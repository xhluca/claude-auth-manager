"""Private registry for Claude subscriptions and provider API keys."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any

from .launcher import find_claude
from .login import run_login
from .paths import (
    account_config_dir,
    account_credential_path,
    claude_config_dir,
    provider_credential_path,
    registry_path,
)
from .storage import atomic_write_json, atomic_write_text, ensure_private_dir, read_json_object

REGISTRY_VERSION = 1
SUPPORTED_KEY_PROVIDERS = frozenset({"openrouter", "google", "anthropic-api"})
ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_.@+-]{0,253})$")
_REFRESH_MARGIN_MS = 5 * 60 * 1000
_account_locks_guard = Lock()
_account_locks: dict[str, Lock] = {}
_native_account_checks: dict[str, tuple[str, int, str]] = {}


def normalize_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.@+-]+", "-", value.strip().casefold()).strip("-_")
    normalized = normalized[:254].rstrip("-_")
    if not normalized or ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError("names must contain letters or numbers and may use . @ + - or _")
    return normalized


def _legacy_id(value: str) -> str:
    """Return the identifier emitted before email punctuation was preserved."""
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().casefold()).strip("-_")
    return normalized[:48].rstrip("-_")


def _empty_registry() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "accounts": {}, "account_aliases": {}, "keys": {}}


def load_registry() -> dict[str, Any]:
    document = read_json_object(registry_path(), missing_ok=True)
    if not document:
        return _empty_registry()
    if document.get("version") != REGISTRY_VERSION:
        raise RuntimeError(f"unsupported credential registry at {registry_path()}")
    accounts = document.get("accounts")
    keys = document.get("keys")
    if not isinstance(accounts, dict) or not isinstance(keys, dict):
        raise RuntimeError(f"invalid credential registry at {registry_path()}")
    aliases = document.setdefault("account_aliases", {})
    if not isinstance(aliases, dict) or not all(
        isinstance(old, str) and isinstance(new, str) for old, new in aliases.items()
    ):
        raise RuntimeError(f"invalid account aliases at {registry_path()}")
    return document


def save_registry(document: dict[str, Any]) -> None:
    atomic_write_json(registry_path(), document)


def list_accounts() -> list[dict[str, Any]]:
    entries = load_registry()["accounts"]
    result = []
    for account_id, entry in sorted(entries.items()):
        account = dict({"id": account_id}, **entry)
        # Read only the plan metadata into the public result. This also picks up
        # refreshed tiers for existing logins without another login or migration.
        try:
            document = read_json_object(
                _account_profile(account_id, entry) / ".credentials.json", missing_ok=True
            )
        except (OSError, RuntimeError):
            document = {}
        oauth = document.get("claudeAiOauth")
        if isinstance(oauth, dict):
            for field, source in (
                ("subscription", "subscriptionType"),
                ("rate_limit_tier", "rateLimitTier"),
            ):
                value = oauth.get(source)
                if isinstance(value, str) and value:
                    account[field] = value
        result.append(account)
    return result


def account_aliases() -> dict[str, str]:
    """Return invisible legacy account IDs accepted for already-running sessions."""
    return dict(load_registry().get("account_aliases", {}))


def _canonical_account_id(account_id: str, document: dict[str, Any]) -> str:
    aliases = document.get("account_aliases", {})
    seen: set[str] = set()
    while account_id in aliases:
        if account_id in seen:
            raise RuntimeError(f"cyclic account alias in {registry_path()}")
        seen.add(account_id)
        account_id = aliases[account_id]
    return account_id


def migrate_email_account_ids() -> dict[str, str]:
    """Replace old lossy email slugs with readable, filesystem-safe email IDs."""
    with _account_lock("_account-id-migration"):
        document = load_registry()
        accounts = document["accounts"]
        mapping: dict[str, str] = {}
        for old, entry in accounts.items():
            email = _email_identity(entry.get("email")) if isinstance(entry, dict) else None
            label = _email_identity(entry.get("label")) if isinstance(entry, dict) else None
            if email and label == email and old == _legacy_id(email):
                new = normalize_id(email)
                if new != old:
                    if new in accounts and new != old:
                        raise RuntimeError(f"cannot migrate account {old}: {new} already exists")
                    mapping[old] = new
        if not mapping:
            return {}

        moved: list[tuple[Path, Path]] = []
        try:
            for old, new in mapping.items():
                entry = accounts[old]
                if entry.get("source") == "native":
                    continue
                source = account_config_dir(old)
                destination = account_config_dir(new)
                if source.is_symlink() or destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"cannot safely migrate account profile {old}")
                if source.exists():
                    source.rename(destination)
                    moved.append((source, destination))
            for old, new in mapping.items():
                accounts[new] = accounts.pop(old)
                document["account_aliases"][old] = new
            save_registry(document)
        except BaseException:
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    destination.rename(source)
            raise
        return mapping


def list_keys(provider: str | None = None) -> list[dict[str, Any]]:
    entries = load_registry()["keys"]
    result = [
        dict({"id": key_id}, **entry)
        for key_id, entry in sorted(entries.items())
        if provider is None or entry.get("provider") == provider
    ]
    return result


def account_entry(account_id: str) -> dict[str, Any]:
    document = load_registry()
    account_id = _canonical_account_id(account_id, document)
    entry = document["accounts"].get(account_id)
    if not isinstance(entry, dict):
        raise RuntimeError(f"Claude account is not configured: {account_id}")
    return entry


def key_entry(key_id: str, *, provider: str | None = None) -> dict[str, Any]:
    entry = load_registry()["keys"].get(key_id)
    if not isinstance(entry, dict):
        raise RuntimeError(f"provider key is not configured: {key_id}")
    if provider is not None and entry.get("provider") != provider:
        raise RuntimeError(f"key {key_id} is not a {provider} key")
    return entry


def _clean_auth_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
    ):
        environment.pop(name, None)
    return environment


def claude_auth_status(config: Path | None = None) -> dict[str, Any]:
    environment = _clean_auth_environment()
    if config is not None:
        environment["CLAUDE_CONFIG_DIR"] = str(config)
    result = subprocess.run(
        [
            find_claude(),
            "--setting-sources",
            "project,local",
            "auth",
            "status",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        stdin=subprocess.DEVNULL,
    )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = result.stderr.strip() or "Claude Code returned invalid authentication status"
        raise RuntimeError(detail[:500]) from exc
    if result.returncode != 0 or not isinstance(status, dict) or status.get("loggedIn") is not True:
        detail = status.get("error") if isinstance(status, dict) else None
        raise RuntimeError(str(detail or "Claude Code is not logged in"))
    if status.get("apiProvider", "firstParty") != "firstParty":
        raise RuntimeError("the selected Claude login is not a first-party subscription")
    return status


def _read_claude_oauth(path: Path) -> dict[str, Any]:
    document = read_json_object(path)
    oauth = document.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise RuntimeError(f"Claude subscription credential not found at {path}")
    access_token = oauth.get("accessToken")
    if (
        not isinstance(access_token, str)
        or not access_token
        or any(c.isspace() for c in access_token)
    ):
        raise RuntimeError(f"invalid Claude subscription credential at {path}")
    return dict(oauth)


def _account_lock(account_id: str) -> Lock:
    with _account_locks_guard:
        return _account_locks.setdefault(account_id, Lock())


def _account_name(name: str | None, email: Any) -> str:
    if isinstance(name, str) and name.strip():
        return name.strip()
    if isinstance(email, str) and email.strip():
        return email.strip()
    raise RuntimeError("Claude did not report an email; pass an explicit account nickname")


def add_current_account(name: str | None = None) -> dict[str, Any]:
    """Register the active native Claude login without duplicating its refresh token."""
    source_dir = claude_config_dir().resolve()
    credential = source_dir / ".credentials.json"
    _read_claude_oauth(credential)
    # Passing CLAUDE_CONFIG_DIR for Claude's ordinary ~/.claude location
    # changes where the CLI looks for ~/.claude.json account metadata. Let the
    # CLI use its true default in that case; pass an override only for an
    # actually custom configuration root.
    default_dir = (Path.home() / ".claude").resolve()
    status_uses_default = source_dir == default_dir
    status = claude_auth_status(None if status_uses_default else source_dir)
    account_name = _account_name(name, status.get("email"))
    account_id = normalize_id(account_name)
    registry = load_registry()
    registry["accounts"][account_id] = {
        "label": account_name,
        "email": status.get("email"),
        "organization": status.get("orgName"),
        "subscription": status.get("subscriptionType"),
        "source": "native",
        "config_dir": str(source_dir),
        "status_uses_default": status_uses_default,
    }
    save_registry(registry)
    return dict({"id": account_id}, **registry["accounts"][account_id])


def add_account_token(name: str | None, token: str, *, email: str | None = None) -> dict[str, Any]:
    """Store a long-lived token produced by ``claude setup-token``."""
    token = token.strip()
    if len(token) < 20 or any(character.isspace() for character in token):
        raise ValueError("the Claude OAuth token has an unexpected format")
    requested_name = name.strip() if isinstance(name, str) and name.strip() else None
    known_email = email.strip() if isinstance(email, str) and email.strip() else None
    account_id = (
        normalize_id(requested_name or known_email)
        if requested_name or known_email
        else f"token-{hashlib.sha256(token.encode()).hexdigest()[:12]}"
    )
    label = requested_name or known_email or account_id
    atomic_write_json(
        account_credential_path(account_id),
        {"claudeAiOauth": {"accessToken": token}},
    )
    registry = load_registry()
    registry["accounts"][account_id] = {
        "label": label,
        "email": known_email,
        "organization": None,
        "subscription": "subscription",
        "source": "setup-token",
    }
    save_registry(registry)
    return dict({"id": account_id}, **registry["accounts"][account_id])


def _email_identity(value: Any) -> str | None:
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _login_destination(entries: dict[str, Any], requested_name: str | None, email: Any) -> str:
    """Reuse identity, not just its lossy slug; never silently replace another account."""
    candidate = normalize_id(_account_name(requested_name, email))
    identity = _email_identity(email)
    if requested_name is None and identity:
        matches = [
            key for key, entry in entries.items() if _email_identity(entry.get("email")) == identity
        ]
        if candidate in matches:
            return candidate
        if len(matches) == 1:
            return matches[0]  # Preserve a previously assigned nickname and all its routes.
        if len(matches) > 1:
            raise RuntimeError(
                "Several saved profiles have this email; use --name to choose which one to update."
            )
    existing = entries.get(candidate)
    if existing is not None:
        previous = _email_identity(existing.get("email"))
        if previous != identity or (previous is None and requested_name is None):
            raise RuntimeError(
                "This account name is already used by a different or unidentified login; "
                "choose a different --name."
            )
    return candidate


def _publish_login(profile: Path, destination: Path, document: dict[str, Any]) -> None:
    """Publish a validated login without removing the directory used by running routes."""
    if destination.is_symlink():
        raise RuntimeError("Refusing to replace a symlinked account profile")
    if not destination.exists():
        profile.rename(destination)
        try:
            save_registry(document)
        except BaseException:
            destination.rename(profile)
            raise
        return

    # Keep per-profile settings and unrelated files. Update Claude's account
    # metadata, then atomically replace the credential file (no missing-file
    # window). Never copy or overwrite the native ~/.claude profile.
    names = [
        name
        for name in (".claude.json", ".config.json", ".credentials.json")
        if (profile / name).is_file()
    ]
    if any((destination / name).is_symlink() for name in names):
        raise RuntimeError("Refusing to replace symlinked account metadata")
    previous = {
        name: (destination / name).read_text(encoding="utf-8")
        if (destination / name).exists()
        else None
        for name in names
    }
    changed: list[str] = []
    try:
        for name in names:
            changed.append(name)
            atomic_write_text(destination / name, (profile / name).read_text(encoding="utf-8"))
        save_registry(document)
    except BaseException:
        for name in reversed(changed):
            old = previous[name]
            if old is None:
                (destination / name).unlink(missing_ok=True)
            else:
                atomic_write_text(destination / name, old)
        raise


def login_account(name: str | None = None) -> dict[str, Any]:
    """Authenticate privately, then add or renew the matching saved Claude account."""
    requested_name = name.strip() if isinstance(name, str) and name.strip() else None
    if requested_name:
        normalize_id(requested_name)  # Reject invalid names before opening the browser.
    # Named logins must also be staged: a cancelled or wrong-account login must
    # not overwrite a working credential before we have verified its identity.
    profile = account_config_dir(f"login-{secrets.token_hex(8)}")
    ensure_private_dir(profile)
    environment = _clean_auth_environment()
    environment["CLAUDE_CONFIG_DIR"] = str(profile)
    command = [find_claude(), "auth", "login", "--claudeai"]
    authenticated = False
    saved = False
    try:
        returncode = run_login(command, env=environment)
        if returncode != 0:
            raise RuntimeError(f"Claude login exited with status {returncode}")
        status = claude_auth_status(profile)
        # Status can refresh credentials. Read the final token, not an earlier
        # snapshot from before that refresh.
        oauth = _read_claude_oauth(profile / ".credentials.json")
        # Drop unrelated per-profile OAuth records and keep only inference auth.
        atomic_write_json(profile / ".credentials.json", {"claudeAiOauth": oauth})
        authenticated = True
        # Serialize in-process publication and reload after the potentially long
        # browser flow, preserving accounts/keys added while login was waiting.
        with _account_lock("_login-publication"):
            document = load_registry()
            entries = document["accounts"]
            account_id = _login_destination(entries, requested_name, status.get("email"))
            previous = entries.get(account_id)
            destination = account_config_dir(account_id)
            if destination.exists() and previous is None:
                raise RuntimeError(
                    "An unregistered profile already uses this account name; "
                    "choose another --name rather than overwriting it."
                )
            label = (
                requested_name
                or (previous or {}).get("label")
                or _account_name(None, status.get("email"))
            )
            entry = dict(previous or {})
            entry.pop("config_dir", None)
            entry.pop("status_uses_default", None)
            entry.update(
                label=label,
                email=status.get("email"),
                organization=status.get("orgName"),
                subscription=status.get("subscriptionType"),
                source="managed",
            )
            entries[account_id] = entry
            with _account_lock(account_id):
                _publish_login(profile, destination, document)
            saved = True
            return dict({"id": account_id, "updated": previous is not None}, **entry)
    except Exception as exc:
        if authenticated and profile.exists():
            raise RuntimeError(
                f"{exc}\nThe authenticated login was kept privately at {profile}; "
                "it is available for recovery without another browser login."
            ) from exc
        raise
    finally:
        if profile.exists() and (saved or not authenticated):
            shutil.rmtree(profile)


def _account_profile(account_id: str, entry: dict[str, Any]) -> Path:
    if entry.get("source") == "native":
        configured = entry.get("config_dir")
        if not isinstance(configured, str) or not configured:
            raise RuntimeError(f"invalid native account path for {account_id}")
        return Path(configured)
    return account_config_dir(account_id)


def _account_status_profile(entry: dict[str, Any], profile: Path) -> Path | None:
    if entry.get("source") == "native" and (
        entry.get("status_uses_default", False)
        or profile.resolve() == (Path.home() / ".claude").resolve()
    ):
        return None
    return profile


def read_account_token(account_id: str) -> str:
    """Return a subscription access token, refreshing through Claude Code when needed."""
    account_id = _canonical_account_id(account_id, load_registry())
    entry = account_entry(account_id)
    profile = _account_profile(account_id, entry)
    status_profile = _account_status_profile(entry, profile)
    path = profile / ".credentials.json"
    with _account_lock(account_id):
        oauth = _read_claude_oauth(path)
        expires_at = oauth.get("expiresAt")
        refreshed_status: dict[str, Any] | None = None
        if (
            isinstance(expires_at, int)
            and expires_at <= int(time.time() * 1000) + _REFRESH_MARGIN_MS
        ):
            if not isinstance(oauth.get("refreshToken"), str):
                raise RuntimeError(f"Claude account {account_id} has expired; log in again")
            # Claude Code owns its OAuth protocol and rotation semantics. Invoking
            # its status command against this isolated profile refreshes and saves
            # the token with the same locking/storage behavior as a normal session.
            refreshed_status = claude_auth_status(status_profile)
            oauth = _read_claude_oauth(path)
            refreshed_expiry = oauth.get("expiresAt")
            if isinstance(refreshed_expiry, int) and refreshed_expiry <= int(time.time() * 1000):
                raise RuntimeError(f"Claude account {account_id} could not be refreshed")
        expected_email = entry.get("email")
        if entry.get("source") == "native" and expected_email:
            # A manual `claude auth login` can replace the native file. Catch it
            # before silently charging a different subscription.
            modified = path.stat().st_mtime_ns
            cached = _native_account_checks.get(account_id)
            cache_value = (str(path), modified, expected_email)
            if refreshed_status is None and cached == cache_value:
                return str(oauth["accessToken"])
            status = refreshed_status or claude_auth_status(status_profile)
            if status.get("email") != expected_email:
                raise RuntimeError(
                    f"native Claude login changed for {account_id}; re-add the account"
                )
            oauth = _read_claude_oauth(path)
            _native_account_checks[account_id] = (
                str(path),
                path.stat().st_mtime_ns,
                expected_email,
            )
        return str(oauth["accessToken"])


def remove_account(account_id: str) -> None:
    registry = load_registry()
    account_id = _canonical_account_id(account_id, registry)
    entry = registry["accounts"].pop(account_id, None)
    if entry is None:
        raise RuntimeError(f"Claude account is not configured: {account_id}")
    registry["account_aliases"] = {
        old: new for old, new in registry.get("account_aliases", {}).items() if new != account_id
    }
    save_registry(registry)
    if isinstance(entry, dict) and entry.get("source") != "native":
        profile = account_config_dir(account_id)
        if profile.exists():
            shutil.rmtree(profile)


def add_key(provider: str, name: str, key: str, *, label: str | None = None) -> dict[str, Any]:
    if provider not in SUPPORTED_KEY_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    key_id = normalize_id(name)
    key = key.strip()
    if len(key) < 10 or any(character.isspace() for character in key):
        raise ValueError(f"the {provider} key has an unexpected format")
    atomic_write_text(provider_credential_path(provider, key_id), f"{key}\n", 0o600)
    registry = load_registry()
    existing = registry["keys"].get(key_id)
    if isinstance(existing, dict) and existing.get("provider") != provider:
        provider_credential_path(provider, key_id).unlink(missing_ok=True)
        raise ValueError(f"key name {key_id} is already used by {existing.get('provider')}")
    registry["keys"][key_id] = {
        "provider": provider,
        "label": (label or name).strip(),
    }
    save_registry(registry)
    return dict({"id": key_id}, **registry["keys"][key_id])


def read_key(key_id: str, *, provider: str | None = None) -> str:
    entry = key_entry(key_id, provider=provider)
    actual_provider = str(entry["provider"])
    path = provider_credential_path(actual_provider, key_id)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"provider credential not found for {key_id}") from exc
    if len(key) < 10 or any(character.isspace() for character in key):
        raise RuntimeError(f"invalid provider credential for {key_id}")
    return key


def remove_key(key_id: str) -> None:
    registry = load_registry()
    entry = registry["keys"].pop(key_id, None)
    if not isinstance(entry, dict):
        raise RuntimeError(f"provider key is not configured: {key_id}")
    save_registry(registry)
    provider_credential_path(str(entry["provider"]), key_id).unlink(missing_ok=True)
