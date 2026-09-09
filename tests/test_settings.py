from __future__ import annotations

import json
import stat

import pytest

from claude_auth_manager.models import claude_model, managed_model
from claude_auth_manager.paths import (
    backup_path,
    claude_settings_path,
    preferences_path,
    router_token_path,
)
from claude_auth_manager.settings import (
    LOCAL_TOKEN_HEADER,
    assert_private_files,
    configure_claude,
    favorite_ids,
    favorite_models,
    reset_integration,
    restore_claude_settings,
    save_preferences,
)


def _write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_configuration_installs_all_credential_scoped_picker_rows(
    isolated_home, managed_models
) -> None:
    original = {
        "theme": "dark",
        "env": {
            "KEEP": "yes",
            "ANTHROPIC_BASE_URL": "https://old.example",
            "ANTHROPIC_CUSTOM_HEADERS": (
                "X-Trace: yes\n"
                "Authorization: Bearer old\n"
                "X-Claude-OpenRouter-Token: legacy-local-token"
            ),
        },
    }
    _write_json(claude_settings_path(), original)

    result = configure_claude(managed_models, port=9555)
    settings = json.loads(result.read_text())

    assert settings["theme"] == "dark"
    assert settings["env"]["KEEP"] == "yes"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9555"
    headers = settings["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Trace: yes" in headers
    assert "Authorization" not in headers
    assert "OpenRouter-Token" not in headers
    assert LOCAL_TOKEN_HEADER in headers
    assert settings["env"]["ANTHROPIC_API_KEY"] == ""
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == ""
    assert settings["env"]["ENABLE_TOOL_SEARCH"] == "false"
    assert settings["model"] == claude_model(managed_models[0])
    rows = settings["modelPicker"]["options"]
    assert {row["model"] for row in rows} == {claude_model(model) for model in managed_models}
    assert {row["label"] for row in rows} == {
        "Sonnet 4.6 (1M context)",
        "Qwen3 Coder",
        "Gemini 3.8 Flash",
    }
    google = next(row for row in rows if row["label"] == "Gemini 3.8 Flash")
    subscription = next(row for row in rows if row["label"] == "Sonnet 4.6 (1M context)")
    assert google["description"] == (
        "Google · Gemini API (google-a) via claude-auth-manager"
    )
    assert subscription["description"] == (
        "Claude · Max 20x (a@example.com) via claude-auth-manager"
    )
    assert settings["modelPicker"]["replaceBuiltInOptions"] is False


def test_configuration_without_native_login_uses_only_loopback_token(
    isolated_home, managed_models
) -> None:
    configure_claude(managed_models[1:], native_login=False)
    settings = json.loads(claude_settings_path().read_text())
    token = router_token_path().read_text().strip()

    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == token
    assert token not in json.dumps(settings["modelPicker"])


def test_reconfigure_keeps_original_backup_and_updates_default_only_if_available(
    isolated_home, managed_models
) -> None:
    original = {"theme": "light", "model": "sonnet"}
    _write_json(claude_settings_path(), original)
    configure_claude(managed_models[:2])
    saved_backup = backup_path().read_text()
    first_default = claude_model(managed_models[0])

    configure_claude([managed_models[2], managed_models[0]])

    assert backup_path().read_text() == saved_backup
    settings = json.loads(claude_settings_path().read_text())
    assert settings["model"] == first_default


def test_preferences_store_route_metadata_but_no_credentials(isolated_home, managed_models) -> None:
    configure_claude(managed_models)
    raw = preferences_path().read_text()
    preferences = json.loads(raw)

    assert preferences["version"] == 3
    assert preferences["mode"] == "manager"
    assert favorite_ids() == [managed_model(model) for model in managed_models]
    assert favorite_models()[2]["credential"] == "google-a"
    assert favorite_models()[0]["subscription"] == "max"
    assert favorite_models()[0]["rate_limit_tier"] == "default_claude_max_20x"
    assert "api-key" not in raw.casefold()
    assert "accessToken" not in raw


def test_context_annotation_preserves_the_existing_default_account(
    isolated_home, managed_models
) -> None:
    first = managed_models[0]
    second = dict(first, credential="account-b", credential_label="b@example.com")
    save_preferences([first, second], managed_model(second))

    configure_claude([first, second])

    settings = json.loads(claude_settings_path().read_text())
    assert settings["model"] == claude_model(second)
    assert favorite_ids() == [managed_model(first), managed_model(second)]


def test_restore_returns_every_managed_setting_to_original(isolated_home, managed_models) -> None:
    original = {
        "theme": "dark",
        "model": "haiku",
        "modelPicker": {"options": [{"model": "custom"}]},
        "hooks": {"PostToolUse": []},
        "env": {
            "ANTHROPIC_BASE_URL": "https://gateway.example",
            "ANTHROPIC_AUTH_TOKEN": "original",
            "ENABLE_TOOL_SEARCH": "true",
        },
    }
    _write_json(claude_settings_path(), original)
    configure_claude(managed_models)

    assert restore_claude_settings() is True
    assert json.loads(claude_settings_path().read_text()) == original


def test_reset_removes_manager_data_but_not_native_account_source(
    isolated_home, managed_models
) -> None:
    original = {"theme": "dark"}
    _write_json(claude_settings_path(), original)
    native_credential = isolated_home / ".claude" / ".credentials.json"
    _write_json(native_credential, {"claudeAiOauth": {"accessToken": "native-token-long"}})
    configure_claude(managed_models)

    assert reset_integration() is True
    assert json.loads(claude_settings_path().read_text()) == original
    assert native_credential.exists()
    assert not preferences_path().exists()


def test_invalid_favorite_without_provider_is_rejected(isolated_home, sample_models) -> None:
    with pytest.raises(ValueError, match="provider and credential"):
        configure_claude([sample_models[2]])


def test_private_file_audit_covers_router_settings(isolated_home, managed_models) -> None:
    configure_claude(managed_models)
    assert_private_files()
    backup_path().chmod(0o644)
    with pytest.raises(RuntimeError, match="insecure permissions"):
        assert_private_files()
    backup_path().chmod(stat.S_IRUSR | stat.S_IWUSR)
    preferences_path().chmod(0o644)
    with pytest.raises(RuntimeError, match="insecure permissions"):
        assert_private_files()
    preferences_path().chmod(stat.S_IRUSR | stat.S_IWUSR)
