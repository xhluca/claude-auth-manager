from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_auth_manager import registry
from claude_auth_manager.paths import (
    account_config_dir,
    account_credential_path,
    accounts_dir,
    claude_config_dir,
    claude_settings_path,
    preferences_path,
    registry_path,
)
from claude_auth_manager.storage import atomic_write_json


@pytest.fixture
def login_stub(monkeypatch):
    state = {"email": "person@example.com", "token": "original-access", "stages": []}

    def run(command, *, env):
        stage = Path(env["CLAUDE_CONFIG_DIR"])
        state["stages"].append(stage)
        atomic_write_json(
            stage / ".credentials.json",
            {
                "claudeAiOauth": {
                    "accessToken": state["token"],
                    "refreshToken": "refresh-" + state["token"],
                    "expiresAt": int(time.time() * 1000) + 3_600_000,
                }
            },
        )
        atomic_write_json(
            stage / ".claude.json",
            {
                "oauthAccount": {
                    "emailAddress": state["email"],
                    "organizationName": state["token"],
                }
            },
        )
        return 0

    monkeypatch.setattr(registry, "find_claude", lambda: "/test/claude")
    monkeypatch.setattr(registry, "run_login", run)
    monkeypatch.setattr(
        registry,
        "claude_auth_status",
        lambda profile=None: {
            "loggedIn": True,
            "apiProvider": "firstParty",
            "email": state["email"],
            "subscriptionType": "max",
            "orgName": "Example organization",
        },
    )
    return state


def test_relogin_updates_same_account_preserving_routes_and_directory(isolated_home, login_stub):
    first = registry.login_account()
    account_id = first["id"]
    assert first["updated"] is False
    directory = account_config_dir(account_id)
    inode = directory.stat().st_ino
    settings = {"model": f"cam/anthropic/{account_id}/claude-sonnet-5[1m]"}
    favorites = {
        "favorites": [{"provider": "anthropic", "credential": account_id, "id": "claude-sonnet-5"}]
    }
    atomic_write_json(claude_settings_path(), settings)
    atomic_write_json(preferences_path(), favorites)
    atomic_write_json(directory / "settings.json", {"theme": "dark"})
    before_settings = claude_settings_path().read_bytes()
    before_favorites = preferences_path().read_bytes()

    login_stub["token"] = "renewed-access"
    second = registry.login_account()

    assert second["id"] == account_id
    assert second["updated"] is True
    assert len(registry.list_accounts()) == 1
    assert registry.read_account_token(account_id) == "renewed-access"
    assert directory.stat().st_ino == inode
    assert (
        json.loads((directory / ".claude.json").read_text())["oauthAccount"]["organizationName"]
        == "renewed-access"
    )
    assert json.loads((directory / "settings.json").read_text()) == {"theme": "dark"}
    assert claude_settings_path().read_bytes() == before_settings
    assert preferences_path().read_bytes() == before_favorites
    assert not list(accounts_dir().glob("login-*"))
    assert stat.S_IMODE(account_credential_path(account_id).stat().st_mode) == 0o600
    assert "updated" not in registry.load_registry()["accounts"][account_id]


@pytest.mark.parametrize("second_name", [None, "Work"])
def test_relogin_keeps_existing_nickname(isolated_home, login_stub, second_name):
    registry.login_account("Work")
    login_stub["email"] = "PERSON@EXAMPLE.COM"
    login_stub["token"] = "renewed-access"

    result = registry.login_account(second_name)

    assert result["id"] == "work"
    assert result["label"] == "Work"
    assert result["updated"] is True
    assert registry.read_account_token("work") == "renewed-access"
    assert len(registry.list_accounts()) == 1


def test_relogin_of_native_account_does_not_replace_native_login(isolated_home, login_stub):
    native = claude_config_dir() / ".credentials.json"
    atomic_write_json(
        native,
        {
            "claudeAiOauth": {
                "accessToken": "native-access",
                "refreshToken": "native-refresh",
            }
        },
    )
    original = native.read_bytes()
    first = registry.add_current_account()

    result = registry.login_account()

    assert result["id"] == first["id"]
    assert result["source"] == "managed"
    assert result["updated"] is True
    assert "config_dir" not in result
    assert "status_uses_default" not in result
    assert native.read_bytes() == original
    assert registry.read_account_token(first["id"]) == "original-access"


def test_relogin_can_upgrade_setup_token_account(isolated_home, login_stub):
    first = registry.add_account_token(
        "Work", "setup-token-that-is-long-enough", email=login_stub["email"]
    )
    result = registry.login_account()
    assert result["id"] == first["id"]
    assert result["source"] == "managed"
    assert registry.read_account_token(first["id"]) == "original-access"


def test_different_email_cannot_take_over_named_route(isolated_home, login_stub):
    login_stub["email"] = "first.last@example.com"
    first = registry.login_account("Work")
    credential = account_credential_path(first["id"])
    before = credential.read_bytes()
    before_registry = registry_path().read_bytes()
    login_stub["email"] = "first-last@example.com"  # Same normalized slug, different identity.
    login_stub["token"] = "different-account-access"

    with pytest.raises(RuntimeError, match="different or unidentified"):
        registry.login_account("Work")

    assert credential.read_bytes() == before
    assert registry_path().read_bytes() == before_registry
    assert (login_stub["stages"][-1] / ".credentials.json").is_file()


def test_email_punctuation_prevents_old_slug_collisions(isolated_home, login_stub):
    login_stub["email"] = "first.last@example.com"
    first = registry.login_account()
    login_stub["email"] = "first-last@example.com"
    second = registry.login_account()

    assert first["id"] == "first.last@example.com"
    assert second["id"] == "first-last@example.com"
    assert len(registry.list_accounts()) == 2


def test_multiple_aliases_need_explicit_target_unless_canonical_exists(isolated_home, login_stub):
    registry.login_account("Work")
    registry.login_account("Second")
    with pytest.raises(RuntimeError, match="Several saved profiles"):
        registry.login_account()
    result = registry.login_account("Work")
    assert result["id"] == "work"
    # An existing canonical email profile wins over alternate nicknames.
    registry.login_account(login_stub["email"])
    assert registry.login_account()["id"] == "person@example.com"


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_failed_named_login_never_touches_old_credentials(
    isolated_home, login_stub, monkeypatch, failure
):
    registry.login_account("Work")
    before = account_credential_path("work").read_bytes()
    before_registry = registry_path().read_bytes()

    def fail(command, *, env):
        profile = Path(env["CLAUDE_CONFIG_DIR"])
        assert profile != account_config_dir("work")
        atomic_write_json(profile / ".credentials.json", {"bad": "partial-login"})
        raise failure

    monkeypatch.setattr(registry, "run_login", fail)
    with pytest.raises(failure):
        registry.login_account("Work")
    assert account_credential_path("work").read_bytes() == before
    assert registry_path().read_bytes() == before_registry
    assert not list(accounts_dir().glob("login-*"))


@pytest.mark.parametrize("existing", [False, True])
def test_failed_registration_rolls_back_and_keeps_authenticated_login(
    isolated_home, login_stub, monkeypatch, existing
):
    if existing:
        registry.login_account()
    before_registry = registry_path().read_bytes() if existing else None
    credential = account_credential_path("person@example.com")
    before = credential.read_bytes() if existing else None
    metadata = credential.parent / ".claude.json"
    old_metadata = metadata.read_bytes() if existing else None
    login_stub["token"] = "retained-access"

    def fail(_document):
        raise OSError("simulated registry write failure")

    monkeypatch.setattr(registry, "save_registry", fail)
    with pytest.raises(RuntimeError, match="kept privately"):
        registry.login_account()

    assert (credential.read_bytes() if credential.exists() else None) == before
    assert (metadata.read_bytes() if metadata.exists() else None) == old_metadata
    assert (registry_path().read_bytes() if registry_path().exists() else None) == before_registry
    pending = login_stub["stages"][-1] / ".credentials.json"
    assert json.loads(pending.read_text())["claudeAiOauth"]["accessToken"] == "retained-access"
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600


def test_metadata_update_failure_restores_old_profile(isolated_home, login_stub, monkeypatch):
    first = registry.login_account()
    folder = account_config_dir(first["id"])
    old_metadata = (folder / ".claude.json").read_bytes()
    old_token = (folder / ".credentials.json").read_bytes()
    login_stub["token"] = "renewed-access"
    real_write = registry.atomic_write_text
    failed = False

    def write(path, value, *args, **kwargs):
        nonlocal failed
        if path == folder / ".credentials.json" and not failed:
            failed = True
            raise OSError("simulated credential write failure")
        return real_write(path, value, *args, **kwargs)

    monkeypatch.setattr(registry, "atomic_write_text", write)
    with pytest.raises(RuntimeError, match="kept privately"):
        registry.login_account()
    assert (folder / ".claude.json").read_bytes() == old_metadata
    assert (folder / ".credentials.json").read_bytes() == old_token


def test_registry_changes_during_browser_login_are_preserved(
    isolated_home, login_stub, monkeypatch
):
    original = registry.run_login

    def run(command, *, env):
        registry.add_key("google", "added-while-waiting", "test-provider-key")
        return original(command, env=env)

    monkeypatch.setattr(registry, "run_login", run)
    registry.login_account()
    assert registry.read_key("added-while-waiting", provider="google") == "test-provider-key"


def test_read_credential_after_status_refresh(isolated_home, login_stub, monkeypatch):
    original = registry.claude_auth_status

    def status(profile):
        result = original(profile)
        atomic_write_json(
            profile / ".credentials.json",
            {
                "claudeAiOauth": {
                    "accessToken": "refreshed-during-status",
                    "refreshToken": "new-refresh",
                }
            },
        )
        return result

    monkeypatch.setattr(registry, "claude_auth_status", status)
    account = registry.login_account()
    assert registry.read_account_token(account["id"]) == "refreshed-during-status"


def test_complete_cli_flow_logs_in_twice_without_duplicate_error(isolated_home, tmp_path):
    """Exercise the real CAM entry point and two separate auth subprocesses."""
    source = Path(__file__).parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "claude").symlink_to(source / "tests/helpers/fake_claude_login.py")
    fake_state = tmp_path / "oauth"
    fake_state.mkdir()
    atomic_write_json(
        preferences_path(),
        {
            "favorites": [
                {
                    "provider": "anthropic",
                    "credential": "person@example.com",
                    "id": "claude-sonnet-5",
                }
            ]
        },
    )
    original_routes = preferences_path().read_bytes()
    env = dict(
        os.environ,
        PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
        PYTHONPATH=str(source / "src"),
        CAM_TEST_LOGIN_DIR=str(fake_state),
        CAM_TEST_LOGIN_MODE="manual",
        BROWSER=str(source / "tests/helpers/capture_browser.py"),
        CAM_TEST_BROWSER_URL=str(fake_state / "browser-url"),
    )
    command = [
        sys.executable,
        "-c",
        "from claude_auth_manager.cli import main; raise SystemExit(main(['account', 'add']))",
    ]
    for token, verb in (("first-token", "Saved"), ("second-token", "Updated")):
        result = subprocess.run(
            command,
            env=dict(env, CAM_TEST_ACCESS_TOKEN=token),
            input="test-private-code#test-login-state\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Login successful." in result.stdout
        assert f"{verb} Claude subscription" in result.stdout
        assert "already exists" not in result.stdout + result.stderr
        assert "test-private-code" not in result.stdout + result.stderr
        assert token not in result.stdout + result.stderr
    assert len(registry.list_accounts()) == 1
    assert registry.read_account_token("person@example.com") == "second-token"
    assert preferences_path().read_bytes() == original_routes
