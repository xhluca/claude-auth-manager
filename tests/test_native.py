from copy import deepcopy

import pytest

from claude_auth_manager import native
from claude_auth_manager.paths import account_config_dir, claude_config_dir, state_dir
from claude_auth_manager.registry import load_registry, save_registry
from claude_auth_manager.storage import atomic_write_json, read_json_object


@pytest.fixture
def logins(isolated_home):
    root = claude_config_dir()
    registry = load_registry()
    for name in ("one", "two"):
        directory = root if name == "one" else account_config_dir(name)
        atomic_write_json(
            directory / ".credentials.json",
            {
                "claudeAiOauth": {
                    "accessToken": "test-" + name,
                    "refreshToken": "refresh-" + name,
                    "expiresAt": 4102444800000,
                    "scopes": ["user:inference", "user:profile"],
                }
            },
        )
        metadata = {"oauthAccount": {"emailAddress": name + "@example.com"}, "theme": "dark"}
        atomic_write_json(
            isolated_home / ".claude.json" if name == "one" else directory / ".claude.json",
            metadata,
        )
        registry["accounts"][name] = {
            "email": name + "@example.com",
            "source": "native" if name == "one" else "managed",
            "config_dir": str(directory),
        }
    save_registry(registry)
    atomic_write_json(root / "settings.json", {"model": "do-not-change"})
    return root


def test_switch_and_switch_back_preserves_accounts_and_settings(logins, isolated_home):
    assert native.use_account("two") == "two"
    assert load_registry()["accounts"]["one"]["source"] == "managed"
    assert (
        read_json_object(account_config_dir("one") / ".credentials.json")["claudeAiOauth"][
            "accessToken"
        ]
        == "test-one"
    )
    assert read_json_object(isolated_home / ".claude.json")["theme"] == "dark"
    assert (
        read_json_object(isolated_home / ".claude.json")["oauthAccount"]["emailAddress"]
        == "two@example.com"
    )
    assert native.use_account("one@example.com") == "one"
    assert (
        read_json_object(logins / ".credentials.json")["claudeAiOauth"]["accessToken"] == "test-one"
    )
    assert read_json_object(logins / "settings.json") == {"model": "do-not-change"}
    assert len(list(state_dir().glob("native-switch-backup-*.json"))) == 2


def test_switch_rejects_setup_tokens_without_mutation(logins):
    path = account_config_dir("two") / ".credentials.json"
    atomic_write_json(path, {"claudeAiOauth": {"accessToken": "setup"}})
    previous = deepcopy(load_registry())
    with pytest.raises(ValueError, match="full login"):
        native.use_account("two")
    assert load_registry() == previous


def test_switch_rolls_back_on_write_error(logins, monkeypatch):
    previous = read_json_object(logins / ".credentials.json")
    original = native.save_registry
    calls = []

    def fail_once(value):
        calls.append(value)
        if len(calls) == 1:
            raise OSError("synthetic disk failure")
        original(value)

    monkeypatch.setattr(native, "save_registry", fail_once)
    with pytest.raises(OSError):
        native.use_account("two")
    assert read_json_object(logins / ".credentials.json") == previous
    assert load_registry()["accounts"]["one"]["source"] == "native"


def test_running_native_sessions_block_switch_without_changes(logins, monkeypatch):
    previous = deepcopy(load_registry())
    monkeypatch.setattr(native, "active_native_sessions", lambda _: True)
    with pytest.raises(RuntimeError, match="close Claude sessions"):
        native.use_account("two")
    assert load_registry() == previous
