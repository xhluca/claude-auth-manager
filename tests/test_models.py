from __future__ import annotations

import pytest

from claude_auth_manager.models import (
    catalog_input_modalities,
    claude_model,
    claude_subscription_label,
    compact_model_name,
    compact_row,
    exact_models,
    input_modalities,
    picker_row,
    search_models,
    supported_parameters,
    supports_parameter,
    supports_tools,
    tool_capability_badge,
)


def ids(models: list[dict[str, object]]) -> list[str]:
    return [str(model["id"]) for model in models]


def test_plain_search_is_case_insensitive_substring_glob(sample_models) -> None:
    result = search_models(sample_models, ["CLAUDE"])
    assert ids(result) == [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
    ]


def test_shell_style_search_matches_each_metadata_field(sample_models) -> None:
    assert ids(search_models(sample_models, ["anthropic/*sonnet*"])) == [
        "anthropic/claude-sonnet-4.6"
    ]
    assert ids(search_models(sample_models, ["*coding model"])) == [
        "qwen/qwen3-coder",
        "anthropic/claude-sonnet-4.6",
    ]


def test_multiple_queries_are_or_patterns(sample_models) -> None:
    assert set(ids(search_models(sample_models, ["gemini", "qwen*"]))) == {
        "google/gemini-3.1-pro-preview",
        "qwen/qwen3-coder",
    }


def test_regex_search_and_error(sample_models) -> None:
    assert ids(search_models(sample_models, [r"^google/.+preview$"], regex=True)) == [
        "google/gemini-3.1-pro-preview"
    ]
    with pytest.raises(ValueError, match="invalid regular expression"):
        search_models(sample_models, ["["], regex=True)


def test_exact_models_preserves_order_and_rejects_unknown(sample_models) -> None:
    selected = exact_models(
        sample_models,
        ["qwen/qwen3-coder", "anthropic/claude-opus-4.6", "qwen/qwen3-coder"],
    )
    assert ids(selected) == ["qwen/qwen3-coder", "anthropic/claude-opus-4.6"]
    with pytest.raises(ValueError, match="not found"):
        exact_models(sample_models, ["missing/model"])


def test_picker_row_has_human_metadata_and_management_marker(sample_models) -> None:
    row = picker_row(sample_models[0])
    assert row["model"] == "anthropic/claude-sonnet-4.6"
    assert row["label"] == "Sonnet 4.6"
    assert row["description"] == (
        "Claude · OpenRouter via claude-auth-manager · "
        "3 in / 15 out / — cached ($/M) · anthropic/claude-sonnet-4.6"
    )
    assert "tools" not in row["description"]
    assert "context" not in row["description"]


def test_managed_picker_label_stays_model_only_and_description_has_route() -> None:
    model = {
        "id": "google/gemini-3.8-flash",
        "name": "Google: Gemini 3.8 Flash",
        "provider": "openrouter",
        "credential": "router-a",
        "credential_label": "OpenRouter work",
        "context_length": 1_048_576,
        "pricing": {
            "prompt": "0.0000005",
            "completion": "0.000003",
            "input_cache_read": "0.000000125",
        },
        "supported_parameters": ["tools", "tool_choice"],
    }

    row = picker_row(model, hybrid=True)

    assert row["label"] == "Gemini 3.8 Flash"
    assert "OpenRouter" not in row["label"]
    assert row["description"] == (
        "Google · OpenRouter (OpenRouter work) via claude-auth-manager · "
        "0.5 in / 3 out / 0.125 cached ($/M) · google/gemini-3.8-flash"
    )
    assert "tools" not in row["description"]
    assert "context" not in row["description"]


def test_picker_prices_are_rounded_without_noisy_trailing_zeroes() -> None:
    row = picker_row(
        {
            "id": "vendor/flash-model",
            "name": "Flash Model",
            "provider": "openrouter",
            "credential": "router",
            "pricing": {
                "prompt": "0.00000004998",
                "completion": "0.00000009996",
                "input_cache_read": "0.000000009996",
            },
        },
        hybrid=True,
    )

    assert "0.05 in / 0.1 out / 0.01 cached ($/M)" in row["description"]


@pytest.mark.parametrize(
    ("value", "rate_limit_tier", "expected"),
    [
        ("max", "default_claude_max_5x", "Max 5x"),
        ("max", "default_claude_max_20x", "Max 20x"),
        ("max", None, "Max"),
        ("max", "unrecognized_tier", "Max"),
        ("pro", "default_claude_max_20x", "Pro"),
        ("team", None, "Team"),
        ("enterprise", None, "Enterprise"),
        ("max_20x", None, "Max 20x"),
        ("subscription", None, "Subscription"),
        (None, None, "Subscription"),
    ],
)
def test_claude_subscription_label(value, rate_limit_tier, expected) -> None:
    assert claude_subscription_label(value, rate_limit_tier) == expected


def test_subscription_picker_description_names_the_plan() -> None:
    model = {
        "id": "claude-opus-5",
        "name": "Claude Opus 5",
        "provider": "anthropic",
        "credential": "account-a",
        "credential_label": "Research account",
        "subscription": "max",
        "rate_limit_tier": "default_claude_max_20x",
        "supported_parameters": ["tools", "tool_choice"],
        "context_length": 1_000_000,
    }

    row = picker_row(model, hybrid=True)

    assert row["label"] == "Opus 5 (1M context)"
    assert row["description"] == ("Claude · Max 20x (Research account) via claude-auth-manager")
    assert row["model"] == "cam/anthropic/account-a/claude-opus-5[1m]"


@pytest.mark.parametrize(
    ("provider", "model_id", "provider_label", "route_label"),
    [
        ("google", "gemini-3.8-flash", "Google", "Gemini API"),
        ("anthropic-api", "claude-sonnet-5", "Claude", "Anthropic API"),
    ],
)
def test_direct_key_descriptions_use_the_uniform_provenance(
    provider, model_id, provider_label, route_label
) -> None:
    row = picker_row(
        {
            "id": model_id,
            "name": model_id,
            "provider": provider,
            "credential": "work",
            "credential_label": "Work key",
            "context_length": 1_000_000,
            "supported_parameters": ["tools"],
        },
        hybrid=True,
    )

    assert row["description"] == (
        f"{provider_label} · {route_label} (Work key) via claude-auth-manager"
    )
    assert "tools" not in row["description"]
    assert "context" not in row["description"]
    assert model_id not in row["description"]


@pytest.mark.parametrize(
    ("model_id", "name", "context", "label", "annotation"),
    [
        ("claude-sonnet-5", "Sonnet 5", 1_000_000, "Sonnet 5 (1M context)", "[1m]"),
        ("claude-sonnet-5", "Sonnet 5", 200_000, "Sonnet 5", ""),
        ("claude-sonnet-4-6", "Sonnet 4.6", 1_000_000, "Sonnet 4.6 (1M context)", "[1m]"),
        ("claude-opus-5", "Opus 5", 1_000_000, "Opus 5 (1M context)", "[1m]"),
        ("claude-opus-4-6", "Opus 4.6", 200_000, "Opus 4.6", ""),
        ("claude-fable-5-1", "Fable 5.1", 1_000_000, "Fable 5.1", "[1m]"),
        ("claude-fable-5", "Fable 5", 1_000_000, "Fable 5", "[1m]"),
        ("claude-haiku-4-5", "Haiku 4.5", 200_000, "Haiku 4.5", ""),
        ("gemini-3.8-flash", "Gemini 3.8 Flash", 1_000_000, "Gemini 3.8 Flash", ""),
    ],
)
def test_context_label_matches_client_selection(model_id, name, context, label, annotation):
    model = {
        "id": model_id,
        "name": name,
        "provider": "google" if model_id.startswith("gemini") else "anthropic",
        "credential": "test-account",
        "context_length": context,
    }
    assert compact_model_name(model) == label
    assert compact_model_name(dict(model, name=label)) == label
    assert claude_model(model).endswith(model_id + annotation)


def test_tool_capabilities_are_explicit_and_unknown_is_not_assumed(sample_models) -> None:
    gemini = sample_models[2]
    qwen = sample_models[3]
    unknown = sample_models[0]

    assert supported_parameters(gemini) == frozenset({"tools", "tool_choice", "max_tokens"})
    assert supports_parameter(gemini, "TOOLS") is True
    assert supports_tools(gemini) is True
    assert tool_capability_badge(gemini, detailed=True) == "tools ✓ · tool choice ✓"
    assert supports_parameter(qwen, "tools") is False
    assert supports_tools(qwen) is False
    assert tool_capability_badge(qwen, detailed=True) == "tools ✗ · tool choice ✗"
    assert supports_parameter(unknown, "tools") is None
    assert supports_tools(unknown) is False
    assert tool_capability_badge(unknown) == "tools ?"


def test_compact_row_exposes_tool_metadata(sample_models) -> None:
    assert compact_row(sample_models[2]).endswith("\t✓\t✓")
    assert compact_row(sample_models[3]).endswith("\t✗\t✗")
    assert compact_row(sample_models[0]).endswith("\t?\t?")


def test_catalog_input_modalities_uses_exact_ids_and_skips_unknown_metadata() -> None:
    models = [
        {
            "id": "text/model",
            "architecture": {"input_modalities": ["Text"]},
        },
        {
            "id": "vision/model",
            "architecture": {"input_modalities": ["text", "IMAGE", "video"]},
        },
        {"id": "unknown/model"},
    ]

    assert input_modalities(models[0]) == frozenset({"text"})
    assert input_modalities(models[2]) is None
    assert catalog_input_modalities(models) == {
        "text/model": frozenset({"text"}),
        "vision/model": frozenset({"text", "image", "video"}),
    }
