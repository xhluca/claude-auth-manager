import pytest

from claude_auth_manager import huggingface


def test_only_free_live_pinned_routes_are_listed():
    models = huggingface.free_models(
        [
            {
                "id": "vendor/model",
                "providers": [
                    {
                        "provider": "free",
                        "status": "live",
                        "pricing": {"input": 0, "output": 0},
                        "supports_tools": True,
                    },
                    {"provider": "paid", "status": "live", "pricing": {"input": 1, "output": 2}},
                    {"provider": "unknown", "status": "live"},
                    {"provider": "offline", "status": "offline", "is_free": True},
                ],
            }
        ]
    )
    assert len(models) == 1
    assert models[0]["id"] == "vendor/model:free"
    assert "tools" in models[0]["supported_parameters"]


def test_price_changes_fail_closed(monkeypatch):
    monkeypatch.setattr(huggingface, "fetch_models", lambda _key: [])
    with pytest.raises(ValueError, match="not currently advertised as free"):
        huggingface.require_free_route("vendor/model:free", "synthetic")
