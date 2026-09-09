from __future__ import annotations

import json

import pytest

from claude_auth_manager import google, openrouter, registry
from claude_auth_manager.catalogs import (
    account_models,
    exact_routes,
    load_all_catalogs,
    load_key_catalog,
    refresh_select_catalogs,
    search_all,
)
from claude_auth_manager.models import managed_model, picker_row
from claude_auth_manager.paths import account_credential_path, catalog_path, registry_path
from claude_auth_manager.registry import add_account_token, add_key
from claude_auth_manager.storage import atomic_write_json


def test_two_openrouter_keys_expose_distinct_routes_for_the_same_model(
    isolated_home, sample_models
) -> None:
    add_key("openrouter", "personal", "openrouter-personal-key")
    add_key("openrouter", "team", "openrouter-team-key")
    openrouter.save_catalog(sample_models, "personal")
    openrouter.save_catalog(sample_models, "team")

    models = load_all_catalogs()
    duplicates = [model for model in models if model["id"] == "qwen/qwen3-coder"]

    assert {model["credential"] for model in duplicates} == {"personal", "team"}
    assert {managed_model(model) for model in duplicates} == {
        "cam/openrouter/personal/qwen/qwen3-coder",
        "cam/openrouter/team/qwen/qwen3-coder",
    }
    with pytest.raises(ValueError, match="ambiguous") as error:
        exact_routes(models, ["qwen/qwen3-coder"])
    assert "openrouter/personal/qwen/qwen3-coder" in str(error.value)
    assert "openrouter/team/qwen/qwen3-coder" in str(error.value)


def test_select_refreshes_each_openrouter_keys_guardrail_catalog(
    isolated_home, sample_models, monkeypatch
) -> None:
    add_key("openrouter", "broad", "openrouter-broad-key")
    add_key("openrouter", "restricted", "openrouter-restricted-key")
    add_key("google", "google", "google-key")
    openrouter.save_catalog(sample_models, "broad")
    openrouter.save_catalog(sample_models, "restricted")
    atomic_write_json(
        catalog_path("google", "google"),
        {"models": [{"id": "gemini-test", "name": "Gemini Test"}]},
    )
    responses = {
        "openrouter-broad-key": sample_models[:2],
        "openrouter-restricted-key": sample_models[1:2],
    }
    calls: list[str] = []

    def guarded_models(key: str):
        calls.append(key)
        return responses[key]

    monkeypatch.setattr(openrouter, "fetch_models", guarded_models)

    models = refresh_select_catalogs()

    assert calls == ["openrouter-broad-key", "openrouter-restricted-key"]
    assert {
        model["id"] for model in models if model.get("credential") == "broad"
    } == {model["id"] for model in sample_models[:2]}
    assert {
        model["id"] for model in models if model.get("credential") == "restricted"
    } == {sample_models[1]["id"]}
    assert any(
        model.get("provider") == "google" and model.get("id") == "gemini-test"
        for model in models
    )
    assert openrouter.load_catalog("restricted") == sample_models[1:2]


def test_select_does_not_fall_back_to_stale_openrouter_catalog(
    isolated_home, sample_models, monkeypatch
) -> None:
    add_key("openrouter", "restricted", "openrouter-restricted-key")
    openrouter.save_catalog(sample_models, "restricted")
    monkeypatch.setattr(
        openrouter,
        "fetch_models",
        lambda _key: (_ for _ in ()).throw(RuntimeError("guardrail refresh failed")),
    )

    with pytest.raises(RuntimeError, match="guardrail refresh failed"):
        refresh_select_catalogs()


def test_exact_routes_accepts_full_or_short_credential_specs(isolated_home, sample_models) -> None:
    add_key("openrouter", "personal", "openrouter-personal-key")
    openrouter.save_catalog(sample_models, "personal")
    models = load_all_catalogs()
    target = next(model for model in models if model["id"] == "qwen/qwen3-coder")

    assert exact_routes(models, ["openrouter/personal/qwen/qwen3-coder"]) == [target]
    assert exact_routes(models, [managed_model(target)]) == [target]
    assert exact_routes(models, ["qwen/qwen3-coder"]) == [target]


def test_cross_provider_catalog_includes_all_accounts_and_keys(
    isolated_home, sample_models
) -> None:
    add_account_token("account-a", "subscription-access-token-long")
    add_account_token("account-b", "another-subscription-token-long")
    add_key("anthropic-api", "anthropic-team", "anthropic-api-key-long")
    add_key("openrouter", "router-a", "openrouter-api-key-long")
    add_key("google", "google-a", "google-api-key-long")
    openrouter.save_catalog([sample_models[3]], "router-a")
    monkey_model = [{"id": "gemini-test", "name": "Gemini Test"}]
    from claude_auth_manager.paths import catalog_path
    from claude_auth_manager.storage import atomic_write_json

    atomic_write_json(catalog_path("google", "google-a"), {"models": monkey_model})

    models = load_all_catalogs()
    providers = {(model["provider"], model["credential"]) for model in models}

    assert ("anthropic", "account-a") in providers
    assert ("anthropic", "account-b") in providers
    assert ("anthropic-api", "anthropic-team") in providers
    assert ("openrouter", "router-a") in providers
    assert ("google", "google-a") in providers


def test_account_catalog_uses_human_label_not_slugged_email(isolated_home) -> None:
    add_account_token(
        "Research account",
        "subscription-access-token-long",
        email="person@example.com",
    )

    models = account_models()

    assert {model["credential"] for model in models} == {"research-account"}
    assert {model["credential_label"] for model in models} == {"Research account"}
    assert any(model["name"] == "Opus 4.8 (1M context)" for model in models)
    assert any(model["name"] == "Fable 5.1" for model in models)


def test_existing_accounts_use_their_own_saved_plan_metadata(isolated_home, monkeypatch) -> None:
    def unexpected_auth(*args, **kwargs):
        raise AssertionError("display metadata must not require authentication")

    monkeypatch.setattr(registry, "claude_auth_status", unexpected_auth)
    for account_id, multiplier in (("one", 5), ("two", 20)):
        add_account_token(account_id, f"subscription-secret-{account_id}-long")
        atomic_write_json(
            account_credential_path(account_id),
            {"claudeAiOauth": {
                "accessToken": f"subscription-secret-{account_id}-long",
                "refreshToken": "private-refresh-token",
                "subscriptionType": "max",
                "rateLimitTier": f"default_claude_max_{multiplier}x",
            }},
        )
    original_registry = registry_path().read_bytes()

    models = account_models()
    rows = [picker_row(model, hybrid=True) for model in models if model["id"] == "claude-opus-5"]

    assert {row["label"] for row in rows} == {"Opus 5 (1M context)"}
    assert {row["description"] for row in rows} == {
        "Claude · Max 5x (one) via claude-auth-manager",
        "Claude · Max 20x (two) via claude-auth-manager",
    }
    assert len({row["model"] for row in rows}) == 2
    assert exact_routes(models, [row["model"] for row in rows]) == [
        model for model in models if model["id"] == "claude-opus-5"
    ]
    assert registry_path().read_bytes() == original_registry
    assert "subscription-secret" not in json.dumps(models)
    assert "private-refresh-token" not in json.dumps(models)


def test_search_matches_provider_key_label_without_losing_original_metadata(
    isolated_home, sample_models
) -> None:
    add_key("openrouter", "work", "openrouter-work-key", label="Lab credits")
    openrouter.save_catalog([sample_models[3]], "work")
    models = load_key_catalog("work")

    found = search_all(models, ["lab credits"])

    assert found == models
    assert found[0]["description"] == "Coding model"


def test_google_catalogs_are_credential_scoped(isolated_home, monkeypatch) -> None:
    add_key("google", "one", "google-key-one")
    add_key("google", "two", "google-key-two")
    responses = {
        "google-key-one": [{"id": "gemini-shared", "name": "One"}],
        "google-key-two": [{"id": "gemini-shared", "name": "Two"}],
    }
    monkeypatch.setattr(google, "fetch_models", lambda key: responses[key])
    google.refresh_catalog("one", "google-key-one")
    google.refresh_catalog("two", "google-key-two")

    models = load_all_catalogs()
    assert {
        (model["credential"], model["name"]) for model in models if model["provider"] == "google"
    } == {("one", "One"), ("two", "Two")}
