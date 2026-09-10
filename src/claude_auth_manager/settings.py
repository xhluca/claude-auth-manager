"""Claude Code settings integration and reversible state management."""

from __future__ import annotations

import fcntl
import secrets
import shutil
import stat
import threading
from functools import wraps
from pathlib import Path
from typing import Any

from .agents import (
    is_managed_hook_group,
    managed_hook_group,
    remove_managed_agents,
    sync_managed_agents,
)
from .fallback import fallback_order, selected_links, validate_links
from .models import claude_model, managed_model, picker_row
from .paths import (
    accounts_dir,
    anthropic_credential_path,
    backup_path,
    cache_dir,
    claude_settings_path,
    config_dir,
    credential_path,
    helper_path,
    launch_settings_path,
    preferences_path,
    provider_keys_dir,
    registry_path,
    router_token_path,
    state_dir,
)
from .storage import atomic_write_json, atomic_write_text, read_json_object

LEGACY_BASE_URL = "https://openrouter.ai/api"
DEFAULT_ROUTER_PORT = 9427
BASE_URL = f"http://127.0.0.1:{DEFAULT_ROUTER_PORT}"
LEGACY_ROOT_FIELDS = ("apiKeyHelper", "modelPicker", "model")
ROOT_FIELDS = (*LEGACY_ROOT_FIELDS, "hooks")
LEGACY_ENV_FIELDS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
VERSION_2_ENV_FIELDS = (*LEGACY_ENV_FIELDS, "ANTHROPIC_CUSTOM_HEADERS")
ENV_FIELDS = (*VERSION_2_ENV_FIELDS, "ENABLE_TOOL_SEARCH")
BACKUP_VERSION = 4
LOCAL_TOKEN_HEADER = "X-Claude-Auth-Manager-Token"
LEGACY_LOCAL_TOKEN_HEADER = "X-Claude-OpenRouter-Token"
CHECK_CONFIRMATION_FIELD = "confirm_billable_checks"

_settings_guard = threading.RLock()
_settings_local = threading.local()


def _settings_locked(function):
    """Serialize CAM read/modify/write operations across router threads and CLI processes."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with _settings_guard:
            if getattr(_settings_local, "held", False):
                return function(*args, **kwargs)
            from .storage import ensure_private_dir

            ensure_private_dir(state_dir())
            with (state_dir() / "settings.lock").open("a") as lock:
                (state_dir() / "settings.lock").chmod(0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                _settings_local.held = True
                try:
                    return function(*args, **kwargs)
                finally:
                    _settings_local.held = False
                    fcntl.flock(lock, fcntl.LOCK_UN)

    return wrapped


def _field_snapshot(document: dict[str, Any], field: str) -> dict[str, Any]:
    if field in document:
        return {"present": True, "value": document[field]}
    return {"present": False}


def _capture_backup(settings: dict[str, Any], existed: bool) -> None:
    path = backup_path()
    if path.exists():
        backup = read_json_object(path)
        if backup.get("settings_path") != str(claude_settings_path()):
            raise RuntimeError(
                "an existing claude-auth-manager backup belongs to a different "
                "CLAUDE_CONFIG_DIR; reset it from that environment first"
            )
        if backup.get("version") == 3:
            root = backup.get("root")
            if not isinstance(root, dict):
                raise RuntimeError(f"invalid settings backup at {path}")
            root["hooks"] = _field_snapshot(settings, "hooks")
            backup["version"] = BACKUP_VERSION
            atomic_write_json(path, backup)
        return
    env = settings.get("env")
    env_object = env if isinstance(env, dict) else {}
    atomic_write_json(
        path,
        {
            "version": BACKUP_VERSION,
            "settings_path": str(claude_settings_path()),
            "settings_existed": existed,
            "root": {field: _field_snapshot(settings, field) for field in ROOT_FIELDS},
            "env_was_object": isinstance(env, dict),
            "env": {field: _field_snapshot(env_object, field) for field in ENV_FIELDS},
        },
    )


def write_key_helper() -> Path:
    path = helper_path()
    content = """#!/bin/sh
set -eu
helper_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
IFS= read -r openrouter_key < "$helper_dir/credential"
printf '%s\\n' "$openrouter_key"
"""
    atomic_write_text(path, content, 0o700)
    return path


def load_preferences() -> dict[str, Any]:
    document = read_json_object(preferences_path(), missing_ok=True)
    favorites = document.get("favorites", [])
    if not isinstance(favorites, list) or not all(
        isinstance(item, (str, dict)) for item in favorites
    ):
        raise RuntimeError(f"invalid favorites in {preferences_path()}")
    confirmation = document.get(CHECK_CONFIRMATION_FIELD, True)
    if not isinstance(confirmation, bool):
        raise RuntimeError(f"invalid billable-check preference in {preferences_path()}")
    return document


def favorite_ids() -> list[str]:
    """Return exact managed route ids (legacy string favorites are preserved)."""
    result: list[str] = []
    for item in load_preferences().get("favorites", []):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            try:
                result.append(managed_model(item))
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(f"invalid favorite route in {preferences_path()}") from None
    return result


def favorite_models() -> list[dict[str, Any]]:
    favorites = load_preferences().get("favorites", [])
    if not favorites:
        return []
    if not all(isinstance(item, dict) for item in favorites):
        raise RuntimeError("legacy favorites must be imported before starting the manager")
    return [dict(item) for item in favorites]


def set_check_confirmation(required: bool) -> None:
    document = load_preferences()
    document["version"] = 3
    document[CHECK_CONFIRMATION_FIELD] = required
    atomic_write_json(preferences_path(), document)


def save_preferences(
    models: list[dict[str, Any]],
    default_model: str,
    *,
    port: int = DEFAULT_ROUTER_PORT,
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("router port must be between 1 and 65535")
    current = load_preferences()
    confirmation = current.get(CHECK_CONFIRMATION_FIELD, True)
    routes = {managed_model(model) for model in models}
    fallbacks = {
        source: [target for target in targets if target in routes]
        for source, targets in selected_links(current).items()
        if source in routes and any(target in routes for target in targets)
    }
    atomic_write_json(
        preferences_path(),
        {
            "version": 3,
            "mode": "manager",
            "fallbacks": fallbacks,
            "favorites": [
                {
                    key: model[key]
                    for key in (
                        "id",
                        "name",
                        "description",
                        "provider",
                        "credential",
                        "credential_label",
                        "subscription",
                        "rate_limit_tier",
                        "context_length",
                        "pricing",
                        "supported_parameters",
                        "architecture",
                    )
                    if key in model
                }
                for model in models
            ],
            "default_model": default_model,
            "router_port": port,
            CHECK_CONFIRMATION_FIELD: confirmation,
        },
    )


@_settings_locked
def save_fallbacks(links: dict[str, list[str]]) -> None:
    document = load_preferences()
    document["fallbacks"] = validate_links(
        links, {managed_model(model) for model in favorite_models()}
    )
    atomic_write_json(preferences_path(), document)
    from .fallback import state_path

    refresh_fallback_picker(read_json_object(state_path(), missing_ok=True).get("active", {}))


@_settings_locked
def refresh_fallback_picker(active: dict[str, Any]) -> None:
    """Annotate CAM-owned rows; leave defaults and non-CAM picker entries intact."""
    from .models import compact_model_name, picker_source

    path = claude_settings_path()
    settings = read_json_object(path, missing_ok=True)
    options = settings.get("modelPicker", {}).get("options", [])
    models = {managed_model(model): model for model in favorite_models()}
    links = selected_links(load_preferences())
    changed = False
    for option in options:
        if not isinstance(option, dict):
            continue
        route = str(option.get("model", "")).removesuffix("[1m]")
        if route not in models:
            continue
        description = picker_row(models[route], hybrid=True)["description"]
        entry = active.get(route, {}) if route in links else {}
        target = entry.get("target")
        reachable = set(fallback_order(route, links)[1:])
        if target is not None and target not in reachable:
            entry, target = {}, None
        if entry.get("exhausted"):
            description = "⚠ Fallback chain exhausted · " + description
        elif target in models:
            model = models[target]
            description = (
                f"⚠ Fallback active → {compact_model_name(model)} — {picker_source(model)}"
            )
        elif route in links:
            model = models[links[route][0]]
            credential = model.get("credential_label") or model["credential"]
            description += f" · fallback → {compact_model_name(model)} ({credential})"
            if len(links[route]) > 1:
                description += f" (+{len(links[route]) - 1})"
        if option.get("description") != description:
            option["description"] = description
            changed = True
    if changed:
        atomic_write_json(path, settings)


def _without_managed_headers(headers: str | None) -> str:
    if not headers:
        return ""
    return "\n".join(
        line
        for line in headers.splitlines()
        if line.partition(":")[0].strip().casefold()
        not in {
            "authorization",
            LOCAL_TOKEN_HEADER.casefold(),
            LEGACY_LOCAL_TOKEN_HEADER.casefold(),
        }
    ).strip()


def _router_headers(headers: str | None, token: str) -> str:
    existing = _without_managed_headers(headers)
    local_auth = f"{LOCAL_TOKEN_HEADER}: {token}"
    return "\n".join(part for part in (existing, local_auth) if part)


def ensure_router_token() -> str:
    path = router_token_path()
    if not path.exists():
        atomic_write_text(path, f"{secrets.token_urlsafe(32)}\n", 0o600)
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(character.isspace() for character in token):
        raise RuntimeError(f"invalid local router token at {path}")
    return token


def _active_backup() -> bool:
    path = backup_path()
    if not path.exists():
        return False
    return read_json_object(path).get("version") in {3, BACKUP_VERSION}


def _install_managed_agent_hook(settings: dict[str, Any]) -> None:
    configured = settings.get("hooks")
    if configured is None:
        hooks: dict[str, Any] = {}
    elif isinstance(configured, dict):
        hooks = dict(configured)
    else:
        raise RuntimeError("the hooks setting in Claude Code settings must be a JSON object")
    configured_pre = hooks.get("PreToolUse")
    if configured_pre is None:
        pre_tool_use: list[Any] = []
    elif isinstance(configured_pre, list):
        pre_tool_use = list(configured_pre)
    else:
        raise RuntimeError("hooks.PreToolUse in Claude Code settings must be a JSON array")
    pre_tool_use = [group for group in pre_tool_use if not is_managed_hook_group(group)]
    pre_tool_use.append(managed_hook_group())
    hooks["PreToolUse"] = pre_tool_use
    settings["hooks"] = hooks


def _remove_managed_agent_hook(settings: dict[str, Any]) -> bool:
    configured = settings.get("hooks")
    if not isinstance(configured, dict):
        return False
    hooks = dict(configured)
    configured_pre = hooks.get("PreToolUse")
    if not isinstance(configured_pre, list):
        return False
    pre_tool_use = [group for group in configured_pre if not is_managed_hook_group(group)]
    if len(pre_tool_use) == len(configured_pre):
        return False
    if pre_tool_use:
        hooks["PreToolUse"] = pre_tool_use
    else:
        hooks.pop("PreToolUse", None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return True


def _has_managed_agent_hook(settings: dict[str, Any]) -> bool:
    configured = settings.get("hooks")
    if not isinstance(configured, dict):
        return False
    pre_tool_use = configured.get("PreToolUse")
    return isinstance(pre_tool_use, list) and any(
        is_managed_hook_group(group) for group in pre_tool_use
    )


@_settings_locked
def refresh_managed_subagents(models: list[dict[str, Any]]) -> Path:
    """Refresh custom agent files and their exact-model guard after package updates."""
    path = claude_settings_path()
    existed = path.exists()
    settings = read_json_object(path, missing_ok=True)
    _capture_backup(settings, existed)
    sync_managed_agents(models)
    _install_managed_agent_hook(settings)
    atomic_write_json(path, settings)
    return path


@_settings_locked
def configure_claude(
    models: list[dict[str, Any]],
    *,
    native_login: bool = True,
    port: int = DEFAULT_ROUTER_PORT,
) -> Path:
    """Route Claude Code through the credential-aware fail-closed router."""
    if not models:
        raise ValueError("select at least one model")
    if not 1 <= port <= 65535:
        raise ValueError("router port must be between 1 and 65535")
    for model in models:
        if not isinstance(model.get("provider"), str) or not isinstance(
            model.get("credential"), str
        ):
            raise ValueError("every favorite must name a provider and credential")
    if not _active_backup():
        migrate_legacy_settings()

    path = claude_settings_path()
    existed = path.exists()
    settings = read_json_object(path, missing_ok=True)
    existing_env = settings.get("env")
    if existing_env is not None and not isinstance(existing_env, dict):
        raise RuntimeError(f"the env setting in {path} must be a JSON object")
    _capture_backup(settings, existed)

    old_preferences = load_preferences()
    old_default = old_preferences.get("default_model")
    default_model = next(
        (
            claude_model(model)
            for model in models
            if old_default in {managed_model(model), claude_model(model)}
        ),
        claude_model(models[0]),
    )

    settings.pop("apiKeyHelper", None)
    env = dict(existing_env or {})
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    # Empty overrides prevent inherited shell credentials from globally
    # shadowing native OAuth. Claude treats empty authentication values as absent.
    env["ANTHROPIC_API_KEY"] = ""
    previous_headers = env.get("ANTHROPIC_CUSTOM_HEADERS")
    if previous_headers is not None and not isinstance(previous_headers, str):
        raise RuntimeError(f"ANTHROPIC_CUSTOM_HEADERS in {path} must be a string")
    token = ensure_router_token()
    if native_login:
        env["ANTHROPIC_AUTH_TOKEN"] = ""
        env["ANTHROPIC_CUSTOM_HEADERS"] = _router_headers(previous_headers, token)
    else:
        # This lets OpenRouter-only sessions reach the local router without a
        # Claude login. Native Claude routes still reject this local-only token.
        env["ANTHROPIC_AUTH_TOKEN"] = token
        env["ANTHROPIC_CUSTOM_HEADERS"] = _router_headers(previous_headers, token)
    # Deferred tool loading is currently rejected by non-Anthropic models in
    # Agent View. Connectors remain authenticated; their tools load eagerly.
    env["ENABLE_TOOL_SEARCH"] = "false"
    settings["env"] = env
    settings["modelPicker"] = {
        "options": [picker_row(model, hybrid=True) for model in models],
        "replaceBuiltInOptions": False,
    }
    settings["model"] = default_model
    sync_managed_agents(models)
    _install_managed_agent_hook(settings)
    atomic_write_json(path, settings)
    launch_settings_path().unlink(missing_ok=True)
    helper_path().unlink(missing_ok=True)
    save_preferences(
        models,
        default_model,
        port=port,
    )
    return path


def refresh_claude_credential(key: str) -> bool:
    """Return whether active hybrid settings will read the new key automatically."""
    del key
    if not _active_backup() or not claude_settings_path().exists():
        return False
    path = claude_settings_path()
    settings = read_json_object(path)
    env = settings.get("env")
    return isinstance(env, dict) and str(env.get("ANTHROPIC_BASE_URL", "")).startswith(
        "http://127.0.0.1:"
    )


def _restore_snapshot(document: dict[str, Any], field: str, snapshot: dict[str, Any]) -> None:
    if snapshot.get("present"):
        document[field] = snapshot.get("value")
    else:
        document.pop(field, None)


def _looks_managed_picker(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    options = value.get("options")
    return isinstance(options, list) and any(
        isinstance(row, dict) and str(row.get("model", "")).startswith("cam/") for row in options
    )


def restore_claude_settings() -> bool:
    path = claude_settings_path()
    if not path.exists():
        remove_managed_agents()
        return False
    settings = read_json_object(path)
    backup_file = backup_path()

    if backup_file.exists():
        backup = read_json_object(backup_file)
        if backup.get("settings_path") != str(path):
            raise RuntimeError(
                "the saved Claude settings backup belongs to a different CLAUDE_CONFIG_DIR"
            )
        root = backup.get("root")
        env_backup = backup.get("env")
        if not isinstance(root, dict) or not isinstance(env_backup, dict):
            raise RuntimeError(f"invalid settings backup at {backup_file}")
        root_fields = ROOT_FIELDS if backup.get("version", 1) >= 4 else LEGACY_ROOT_FIELDS
        for field in root_fields:
            snapshot = root.get(field)
            if not isinstance(snapshot, dict):
                raise RuntimeError(f"invalid settings backup field {field}")
            _restore_snapshot(settings, field, snapshot)

        env = settings.get("env")
        if env is not None and not isinstance(env, dict):
            raise RuntimeError(f"the env setting in {path} must be a JSON object")
        env_object = dict(env or {})
        backup_version = backup.get("version", 1)
        if backup_version >= 3:
            fields = ENV_FIELDS
        elif backup_version == 2:
            fields = VERSION_2_ENV_FIELDS
        else:
            fields = LEGACY_ENV_FIELDS
        for field in fields:
            snapshot = env_backup.get(field)
            if not isinstance(snapshot, dict):
                raise RuntimeError(f"invalid environment backup field {field}")
            _restore_snapshot(env_object, field, snapshot)
        if env_object or backup.get("env_was_object"):
            settings["env"] = env_object
        else:
            settings.pop("env", None)
    else:
        managed_helper = settings.get("apiKeyHelper") == str(helper_path().resolve())
        managed_picker = _looks_managed_picker(settings.get("modelPicker"))
        picker = settings.get("modelPicker")
        picker_options = picker.get("options", []) if isinstance(picker, dict) else []
        managed_model_ids = {row.get("model") for row in picker_options if isinstance(row, dict)}
        managed_model = settings.get("model") in managed_model_ids
        env = settings.get("env")
        managed_headers = (
            isinstance(env, dict)
            and isinstance(env.get("ANTHROPIC_CUSTOM_HEADERS"), str)
            and LOCAL_TOKEN_HEADER.casefold() in env["ANTHROPIC_CUSTOM_HEADERS"].casefold()
        )
        managed_hook = _remove_managed_agent_hook(settings)
        managed_base = isinstance(env, dict) and (
            env.get("ANTHROPIC_BASE_URL") == LEGACY_BASE_URL
            or (
                str(env.get("ANTHROPIC_BASE_URL", "")).startswith("http://127.0.0.1:")
                and (managed_picker or managed_headers)
            )
        )
        if not (managed_helper or managed_picker or managed_base or managed_hook):
            remove_managed_agents()
            return False
        if managed_helper:
            settings.pop("apiKeyHelper", None)
        if managed_picker:
            settings.pop("modelPicker", None)
        if managed_model:
            settings.pop("model", None)
        if isinstance(env, dict) and (managed_helper or managed_picker or managed_base):
            env = dict(env)
            if managed_base:
                env.pop("ANTHROPIC_BASE_URL", None)
            for field in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                if env.get(field) == "":
                    env.pop(field, None)
            if managed_base and isinstance(env.get("ANTHROPIC_CUSTOM_HEADERS"), str):
                headers = _without_managed_headers(env["ANTHROPIC_CUSTOM_HEADERS"])
                if headers:
                    env["ANTHROPIC_CUSTOM_HEADERS"] = headers
                else:
                    env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
            if managed_base and env.get("ENABLE_TOOL_SEARCH") in {"true", "false"}:
                env.pop("ENABLE_TOOL_SEARCH", None)
            if env:
                settings["env"] = env
            else:
                settings.pop("env", None)

    if settings:
        atomic_write_json(path, settings)
    else:
        path.unlink()
    remove_managed_agents()
    return True


def migrate_legacy_settings() -> bool:
    """Restore the pre-0.2 global integration and retire its auth helper."""
    legacy_present = helper_path().exists()
    if backup_path().exists():
        legacy_present = read_json_object(backup_path()).get("version", 1) != BACKUP_VERSION
    if not legacy_present and claude_settings_path().exists():
        settings = read_json_object(claude_settings_path())
        env = settings.get("env")
        managed_headers = (
            isinstance(env, dict)
            and isinstance(env.get("ANTHROPIC_CUSTOM_HEADERS"), str)
            and LOCAL_TOKEN_HEADER.casefold() in env["ANTHROPIC_CUSTOM_HEADERS"].casefold()
        )
        legacy_present = (
            settings.get("apiKeyHelper") == str(helper_path().resolve())
            or _looks_managed_picker(settings.get("modelPicker"))
            or _has_managed_agent_hook(settings)
            or (
                isinstance(env, dict)
                and (
                    env.get("ANTHROPIC_BASE_URL") == LEGACY_BASE_URL
                    or (
                        str(env.get("ANTHROPIC_BASE_URL", "")).startswith("http://127.0.0.1:")
                        and managed_headers
                    )
                )
            )
        )
    if not legacy_present:
        return False
    restored = restore_claude_settings()
    backup_path().unlink(missing_ok=True)
    helper_path().unlink(missing_ok=True)
    launch_settings_path().unlink(missing_ok=True)
    return restored


def _remove_private_tree(path: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved in {home, Path("/")} or len(resolved.parts) < 3:
        raise RuntimeError(f"refusing to remove unsafe path: {resolved}")
    shutil.rmtree(resolved)


def reset_integration() -> bool:
    restored = restore_claude_settings()
    for directory in (config_dir(), cache_dir(), state_dir()):
        _remove_private_tree(directory)
    return restored


def assert_private_files() -> None:
    """Raise if a secret-bearing file is accessible by group or other users."""
    paths = [
        credential_path(),
        anthropic_credential_path(),
        backup_path(),
        router_token_path(),
        preferences_path(),
        registry_path(),
        helper_path(),
        launch_settings_path(),
    ]
    if _active_backup():
        paths.append(claude_settings_path())
    for directory in (accounts_dir(), provider_keys_dir()):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    for path in paths:
        if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f"insecure permissions on {path}")
