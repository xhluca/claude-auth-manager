import json

import pytest

from claude_auth_manager import catalogs, cli, subscriptions
from claude_auth_manager.registry import add_account_token
from claude_auth_manager.storage import atomic_write_json, read_json_object

TABLE = """| Feature | Claude Opus 5.5 | Claude Sonnet 5.5 |
| --- | --- | --- |
| Claude API ID | `claude-opus-5-5` | `claude-sonnet-5-5` |
| Context window | 1M tokens | 200K tokens |
| Amazon Bedrock ID | `claude-fake-provider-id` | `claude-ignore-this-id` |

This example mentions `claude-not-a-real-model` outside the model table.
"""


def test_pagination_discovers_new_model_and_deduplicates(monkeypatch):
    monkeypatch.setattr(subscriptions, "read_account_token", lambda _: "synthetic-secret")
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        return json.dumps(
            {
                "data": [
                    {
                        "id": "claude-opus-5-5",
                        "display_name": "Claude Opus 5.5",
                        "max_input_tokens": 1000000,
                    }
                ],
                "has_more": len(calls) == 1,
                "last_id": "claude-opus-5-5",
            }
        ).encode()

    monkeypatch.setattr(subscriptions, "_get", get)
    models = subscriptions.fetch_account_models("account")
    assert len(models) == 1
    assert models[0]["context_length"] == 1000000
    assert "after_id=claude-opus-5-5" in calls[1][0]
    assert calls[0][1]["Authorization"] == "Bearer synthetic-secret"
    assert "synthetic-secret" not in json.dumps(models)


def test_repeated_cursor_is_bounded(monkeypatch):
    monkeypatch.setattr(subscriptions, "read_account_token", lambda _: "synthetic")
    monkeypatch.setattr(
        subscriptions, "_get", lambda *_: b'{"data": [], "has_more": true, "last_id": "same"}'
    )
    with pytest.raises(ValueError, match="pagination"):
        subscriptions.fetch_account_models("account")


def test_public_table_parsing_accepts_only_official_api_columns():
    models = subscriptions.parse_public_models(TABLE)
    assert [model["id"] for model in models] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert [model["context_length"] for model in models] == [1000000, 200000]
    with pytest.raises(ValueError):
        subscriptions.parse_public_models("`claude-opus-5-5` mentioned in a random sentence")


def test_select_refreshes_accounts_and_offline_uses_cache(isolated_home, monkeypatch):
    add_account_token("first", "synthetic-account-token")
    add_account_token("second", "another-synthetic-token")
    monkeypatch.setattr(
        subscriptions,
        "fetch_account_models",
        lambda _: [
            {
                "id": "claude-opus-5-5",
                "name": "Claude Opus 5.5",
                "context_length": 1000000,
            }
        ],
    )
    models = catalogs.refresh_select_catalogs()
    assert {model["credential"] for model in models} == {"first", "second"}
    assert {model["id"] for model in models} == {"claude-opus-5-5"}

    def no_network(*_):
        pytest.fail("offline listing attempted discovery")

    monkeypatch.setattr(subscriptions, "fetch_account_models", no_network)
    monkeypatch.setattr(subscriptions, "fetch_public_models", no_network)
    assert catalogs.load_all_catalogs() == models


def test_fallback_public_lookup_shared_and_does_not_erase_older_models(
    isolated_home, monkeypatch, capsys
):
    for name in ("first", "second"):
        add_account_token(name, "synthetic-account-token")

    def unavailable(_):
        raise RuntimeError("synthetic expired credentials")

    monkeypatch.setattr(subscriptions, "fetch_account_models", unavailable)
    calls = []

    def public():
        calls.append(True)
        return subscriptions.parse_public_models(TABLE)

    monkeypatch.setattr(subscriptions, "fetch_public_models", public)
    models = catalogs.refresh_all_catalogs()
    assert len(calls) == 1
    assert {"claude-opus-5-5", "claude-opus-5"} <= {model["id"] for model in models}
    assert {model["catalog_source"] for model in models} == {"public", "cached"}
    assert "access is not verified" in capsys.readouterr().err
    saved = read_json_object(subscriptions.cache_path("first"))
    assert "synthetic-account-token" not in json.dumps(saved)
    assert saved["source"] == subscriptions.PUBLIC_URL


def test_failed_refresh_keeps_last_good_catalog(isolated_home, monkeypatch):
    path = subscriptions.cache_path("account")
    atomic_write_json(path, {"models": [{"id": "claude-opus-5-5", "name": "Opus 5.5"}]})
    original = path.read_bytes()

    def unavailable(*_):
        raise ValueError("bad response")

    monkeypatch.setattr(subscriptions, "fetch_account_models", unavailable)
    monkeypatch.setattr(subscriptions, "fetch_public_models", unavailable)
    models, source = subscriptions.refresh_catalog("account", {}, catalogs.ANTHROPIC_MODELS)
    assert models[0]["id"] == "claude-opus-5-5"
    assert source == "cached/bundled"
    assert path.read_bytes() == original


def test_bootstrap_handles_catalog_without_previous_sonnet(isolated_home, monkeypatch):
    add_account_token("account", "synthetic-account-token")
    atomic_write_json(
        subscriptions.cache_path("account"),
        {"models": [{"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"}]},
    )
    configured = []
    monkeypatch.setattr(cli, "_configure", lambda models, _: configured.extend(models))
    assert cli._bootstrap_account("account")
    assert configured[0]["id"] == "claude-sonnet-5-5"
