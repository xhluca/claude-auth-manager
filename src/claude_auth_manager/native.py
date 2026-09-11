"""Explicit native-login switching; preserve routed accounts and unrelated settings."""

from __future__ import annotations

import fcntl
import os
import secrets
import sys
from copy import deepcopy
from pathlib import Path

from .paths import account_config_dir, claude_config_dir, state_dir
from .registry import (
    _account_profile,
    _canonical_account_id,
    _read_claude_oauth,
    load_registry,
    save_registry,
)
from .storage import atomic_write_json, ensure_private_dir, read_json_object


def active_native_sessions(profile: Path) -> bool:
    """Do not replace OAuth underneath processes that may refresh that same file."""
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            argv = (process / "cmdline").read_bytes().split(b"\0")
            executable = os.readlink(process / "exe")
            is_claude = "claude/versions/" in executable or any(
                arg.endswith((b"/claude", b"/claude-code/cli.js")) for arg in argv[:2]
            )
            if not is_claude:
                continue
            env = dict(
                value.split(b"=", 1)
                for value in (process / "environ").read_bytes().split(b"\0")
                if b"=" in value
            )
            root = env.get(b"CLAUDE_CONFIG_DIR") or env.get(b"HOME", b"") + b"/.claude"
            if Path(os.fsdecode(root)).resolve() == profile:
                return True
        except (OSError, ValueError):
            continue
    return False


def use_account(name: str) -> str:
    """Switch file-backed Claude OAuth, retaining a private recovery snapshot."""
    if sys.platform != "linux":
        raise RuntimeError(
            "native account switching currently supports Linux file-backed logins only"
        )
    root = state_dir()
    ensure_private_dir(root)
    with (root / "native-switch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = load_registry()
        account_id = _canonical_account_id(name, registry)
        if account_id not in registry["accounts"]:
            matches = [
                key
                for key, value in registry["accounts"].items()
                if name in {value.get("label"), value.get("email")}
            ]
            if len(matches) != 1:
                raise ValueError("unknown or ambiguous Claude account")
            account_id = matches[0]
        entry = registry["accounts"][account_id]
        native = claude_config_dir().resolve()
        source = _account_profile(account_id, entry)
        if source.resolve() == native:
            return account_id
        if active_native_sessions(native):
            raise RuntimeError(
                "close Claude sessions using the native profile before switching its login; "
                "managed /model switching remains hot"
            )
        oauth = _read_claude_oauth(source / ".credentials.json")
        if not oauth.get("refreshToken"):
            raise ValueError("native account switching requires a full login, not a setup token")
        metadata = read_json_object(source / ".claude.json", missing_ok=True)
        if not metadata.get("oauthAccount"):
            metadata = read_json_object(source / ".config.json", missing_ok=True)
        identity = metadata.get("oauthAccount")
        if not isinstance(identity, dict) or not identity.get("emailAddress"):
            raise ValueError(
                "saved account has no native profile metadata; renew it with cam account add"
            )
        if entry.get("email") and identity["emailAddress"] != entry["email"]:
            raise ValueError("saved account identity does not match its profile metadata")
        native_meta = (
            Path.home() / ".claude.json"
            if native == (Path.home() / ".claude").resolve()
            else native / ".claude.json"
        )
        native_credential = native / ".credentials.json"
        old_credentials = read_json_object(native_credential, missing_ok=True)
        old_metadata = read_json_object(native_meta, missing_ok=True)
        native_ids = [
            key
            for key, value in registry["accounts"].items()
            if value.get("source") == "native" and _account_profile(key, value).resolve() == native
        ]
        if old_credentials.get("claudeAiOauth") and not native_ids:
            raise ValueError("save the current login first with cam account add --current")
        current_email = old_metadata.get("oauthAccount", {}).get("emailAddress")
        if any(registry["accounts"][key].get("email") != current_email for key in native_ids):
            raise ValueError(
                "native login identity changed; re-register it with cam account add --current"
            )
        writes = {}
        updated = deepcopy(registry)
        for key in native_ids:
            destination = account_config_dir(key)
            writes[destination / ".credentials.json"] = old_credentials
            writes[destination / ".claude.json"] = {
                "oauthAccount": old_metadata.get("oauthAccount")
            }
            updated["accounts"][key]["source"] = "managed"
            updated["accounts"][key].pop("config_dir", None)
            updated["accounts"][key].pop("status_uses_default", None)
        writes[native_meta] = {**old_metadata, "oauthAccount": identity}
        writes[native_credential] = {**old_credentials, "claudeAiOauth": oauth}
        if any(path.is_symlink() or path.parent.is_symlink() for path in writes):
            raise ValueError("refusing to overwrite symlinked native/account files")
        previous = {path: read_json_object(path) if path.exists() else None for path in writes}
        atomic_write_json(
            root / f"native-switch-backup-{secrets.token_hex(8)}.json",
            {
                "registry": registry,
                "files": {str(path): value for path, value in previous.items()},
            },
        )
        updated["accounts"][account_id].update(
            source="native",
            config_dir=str(native),
            status_uses_default=native_meta.parent != native,
        )
        try:
            for path, value in writes.items():
                atomic_write_json(path, value)
            save_registry(updated)
        except BaseException:
            for path, value in previous.items():
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_json(path, value)
            save_registry(registry)
            raise
        return account_id
