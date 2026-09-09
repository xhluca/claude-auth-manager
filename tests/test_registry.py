from __future__ import annotations

import json
import stat
import subprocess
import time
from pathlib import Path

import pytest

from claude_auth_manager import registry
from claude_auth_manager.paths import (
    account_config_dir,
    account_credential_path,
    claude_config_dir,
    provider_credential_path,
    registry_path,
)


def _write_oauth(path: Path, token: str, **extra: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, **extra}}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Work Account ", "work-account"),
        ("A_b-2", "a_b-2"),
        ("Person+Lab@Example.COM", "person+lab@example.com"),
    ],
)
def test_normalize_id_produces_safe_names(raw: str, expected: str) -> None:
    assert registry.normalize_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "---", "../../", " "])
def test_normalize_id_rejects_empty_or_path_only_names(raw: str) -> None:
    with pytest.raises(ValueError):
        registry.normalize_id(raw)


def test_multiple_keys_are_stored_independently_with_private_permissions(isolated_home) -> None:
    registry.add_key("openrouter", "personal", "openrouter-private-one")
    registry.add_key("openrouter", "team", "openrouter-private-two")
    registry.add_key("google", "gemini", "google-private-three")

    assert registry.read_key("personal", provider="openrouter") == "openrouter-private-one"
    assert registry.read_key("team", provider="openrouter") == "openrouter-private-two"
    assert registry.read_key("gemini", provider="google") == "google-private-three"
    assert [entry["id"] for entry in registry.list_keys("openrouter")] == ["personal", "team"]
    for provider, key_id in (
        ("openrouter", "personal"),
        ("openrouter", "team"),
        ("google", "gemini"),
    ):
        mode = stat.S_IMODE(provider_credential_path(provider, key_id).stat().st_mode)
        assert mode == 0o600
    assert stat.S_IMODE(registry_path().stat().st_mode) == 0o600


def test_key_names_are_unique_across_providers_and_do_not_overwrite(isolated_home) -> None:
    registry.add_key("openrouter", "work", "openrouter-private-key")
    with pytest.raises(ValueError, match="already used"):
        registry.add_key("google", "work", "google-private-key")
    assert registry.read_key("work", provider="openrouter") == "openrouter-private-key"
    assert not provider_credential_path("google", "work").exists()


def test_add_current_references_native_credentials_without_copying_refresh_token(
    isolated_home, monkeypatch
) -> None:
    native = claude_config_dir() / ".credentials.json"
    _write_oauth(
        native,
        "native-access-token-long",
        refreshToken="native-refresh-secret",
        subscriptionType="max",
        rateLimitTier="default_claude_max_20x",
    )
    monkeypatch.setattr(
        registry,
        "claude_auth_status",
        lambda config=None: {
            "loggedIn": True,
            "apiProvider": "firstParty",
            "email": "person@example.com",
            "orgName": "Example",
            "subscriptionType": "max",
        },
    )

    entry = registry.add_current_account("Personal")

    assert entry["id"] == "personal"
    assert entry["source"] == "native"
    assert registry.read_account_token("personal") == "native-access-token-long"
    assert not account_credential_path("personal").exists()
    assert "native-refresh-secret" not in registry_path().read_text()
    listed = registry.list_accounts()
    assert listed[0]["rate_limit_tier"] == "default_claude_max_20x"
    assert "native-access-token-long" not in json.dumps(listed)
    assert "native-refresh-secret" not in json.dumps(listed)


def test_add_current_defaults_name_and_id_to_authenticated_email(
    isolated_home, monkeypatch
) -> None:
    native = claude_config_dir() / ".credentials.json"
    _write_oauth(native, "native-access-token-long")
    monkeypatch.setattr(
        registry,
        "claude_auth_status",
        lambda config=None: {
            "loggedIn": True,
            "apiProvider": "firstParty",
            "email": "person@example.com",
            "subscriptionType": "max",
        },
    )

    entry = registry.add_current_account()

    assert entry["id"] == "person@example.com"
    assert entry["label"] == "person@example.com"
    assert entry["email"] == "person@example.com"


def test_native_account_change_fails_closed(isolated_home, monkeypatch) -> None:
    native = claude_config_dir() / ".credentials.json"
    _write_oauth(native, "native-access-token-long")
    statuses = iter(
        [
            {"loggedIn": True, "email": "a@example.com", "apiProvider": "firstParty"},
            {"loggedIn": True, "email": "b@example.com", "apiProvider": "firstParty"},
        ]
    )
    monkeypatch.setattr(registry, "claude_auth_status", lambda config=None: next(statuses))
    registry.add_current_account("native")

    with pytest.raises(RuntimeError, match="native Claude login changed"):
        registry.read_account_token("native")


def test_expiring_managed_account_refreshes_through_claude_cli(isolated_home, monkeypatch) -> None:
    registry.add_account_token("secondary", "setup-token-that-is-long-enough")
    path = account_credential_path("secondary")
    _write_oauth(
        path,
        "expired-access-token-long",
        refreshToken="refresh-token-long",
        expiresAt=int(time.time() * 1000) - 1000,
    )
    calls: list[Path | None] = []

    def refresh(config: Path | None = None) -> dict[str, object]:
        calls.append(config)
        _write_oauth(
            path,
            "refreshed-access-token-long",
            refreshToken="rotated-refresh-token-long",
            expiresAt=int(time.time() * 1000) + 3_600_000,
        )
        return {"loggedIn": True, "apiProvider": "firstParty"}

    monkeypatch.setattr(registry, "claude_auth_status", refresh)
    assert registry.read_account_token("secondary") == "refreshed-access-token-long"
    assert calls == [path.parent]


def test_setup_token_without_name_gets_stable_non_secret_id(isolated_home) -> None:
    token = "setup-token-that-is-long-enough"

    first = registry.add_account_token(None, token)
    second = registry.add_account_token(None, token)

    assert first["id"] == second["id"]
    assert first["id"].startswith("token-")
    assert first["label"] == first["id"]
    assert token not in registry_path().read_text()
    assert registry.read_account_token(first["id"]) == token


def test_migrate_legacy_email_slug_preserves_profile_and_route_alias(isolated_home) -> None:
    old = "person-example-com"
    new = "person@example.com"
    registry.save_registry(
        {
            "version": registry.REGISTRY_VERSION,
            "accounts": {
                old: {
                    "label": new,
                    "email": new,
                    "source": "managed",
                    "subscription": "max",
                }
            },
            "keys": {},
        }
    )
    _write_oauth(account_credential_path(old), "managed-access-token-long")

    assert registry.migrate_email_account_ids() == {old: new}

    assert [entry["id"] for entry in registry.list_accounts()] == [new]
    assert not account_config_dir(old).exists()
    assert account_credential_path(new).is_file()
    assert registry.account_aliases() == {old: new}
    assert registry.read_account_token(old) == "managed-access-token-long"


def test_removing_managed_account_removes_profile_but_native_source_survives(
    isolated_home, monkeypatch
) -> None:
    registry.add_account_token("managed", "setup-token-that-is-long-enough")
    native = claude_config_dir() / ".credentials.json"
    _write_oauth(native, "native-access-token-long")
    monkeypatch.setattr(
        registry,
        "claude_auth_status",
        lambda config=None: {
            "loggedIn": True,
            "apiProvider": "firstParty",
            "email": "a@example.com",
        },
    )
    registry.add_current_account("native")

    registry.remove_account("managed")
    registry.remove_account("native")

    assert not account_credential_path("managed").exists()
    assert native.exists()


def test_supplemental_login_uses_isolated_profile_and_keeps_only_claude_oauth(
    isolated_home, monkeypatch
) -> None:
    native = claude_config_dir() / ".credentials.json"
    _write_oauth(native, "native-access-token-long", refreshToken="native-refresh")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **kwargs: object) -> int:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        calls.append((command, environment))
        profile = Path(environment["CLAUDE_CONFIG_DIR"])
        profile.mkdir(parents=True, exist_ok=True)
        (profile / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "supplemental-access-token",
                        "refreshToken": "supplemental-refresh-token",
                    },
                    "unrelated": {"secret": "remove-me"},
                }
            )
        )
        return 0

    monkeypatch.setattr(registry, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(registry, "run_login", run)
    monkeypatch.setattr(
        registry,
        "claude_auth_status",
        lambda config=None: {
            "loggedIn": True,
            "apiProvider": "firstParty",
            "email": "second@example.com",
            "subscriptionType": "max",
        },
    )

    entry = registry.login_account("Second")

    assert entry["id"] == "second"
    command, environment = calls[0]
    assert command == [
        "/usr/bin/claude",
        "auth",
        "login",
        "--claudeai",
    ]
    staged = Path(environment["CLAUDE_CONFIG_DIR"])
    assert staged.name.startswith("login-")
    assert not staged.exists()
    stored = json.loads(account_credential_path("second").read_text())
    assert set(stored) == {"claudeAiOauth"}
    assert json.loads(native.read_text())["claudeAiOauth"]["accessToken"] == (
        "native-access-token-long"
    )


def test_supplemental_login_without_name_uses_authenticated_email(
    isolated_home, monkeypatch
) -> None:
    used_profile: Path | None = None

    def run(command: list[str], **kwargs: object) -> int:
        nonlocal used_profile
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        used_profile = Path(environment["CLAUDE_CONFIG_DIR"])
        _write_oauth(used_profile / ".credentials.json", "supplemental-access-token")
        return 0

    monkeypatch.setattr(registry, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(registry, "run_login", run)
    monkeypatch.setattr(
        registry,
        "claude_auth_status",
        lambda config=None: {
            "loggedIn": True,
            "apiProvider": "firstParty",
            "email": "second@example.com",
            "subscriptionType": "max",
        },
    )

    entry = registry.login_account()

    assert entry["id"] == "second@example.com"
    assert entry["label"] == "second@example.com"
    assert entry["email"] == "second@example.com"
    assert used_profile is not None
    assert used_profile.name.startswith("login-")
    assert not used_profile.exists()
    assert account_credential_path("second@example.com").exists()


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_cancelled_login_removes_only_its_temporary_profile(
    isolated_home, monkeypatch, failure
) -> None:
    native = claude_config_dir() / ".credentials.json"
    _write_oauth(native, "native-access-token-long")
    registry.add_account_token("existing", "existing-account-token")
    before_registry = registry_path().read_bytes()
    before_native = native.read_bytes()

    def fail(command, *, env):
        Path(env["CLAUDE_CONFIG_DIR"]).joinpath("partial-login").touch()
        raise failure

    monkeypatch.setattr(registry, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(registry, "run_login", fail)
    with pytest.raises(failure):
        registry.login_account()
    assert registry_path().read_bytes() == before_registry
    assert native.read_bytes() == before_native
    assert registry.read_account_token("existing") == "existing-account-token"
    assert not list(account_credential_path("existing").parent.parent.glob("login-*"))


def test_auth_status_removes_inherited_provider_overrides(isolated_home, monkeypatch) -> None:
    captured: dict[str, object] = {}
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
    ):
        monkeypatch.setenv(name, "must-not-leak")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "apiProvider": "firstParty",
                    "email": "person@example.com",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(registry, "find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(registry.subprocess, "run", run)
    status = registry.claude_auth_status(claude_config_dir())

    assert status["email"] == "person@example.com"
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert not any(
        name in environment
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "CLAUDE_CODE_OAUTH_SCOPES",
        )
    )
