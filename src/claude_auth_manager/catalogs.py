"""Unified model catalog across subscriptions and API-key providers."""

from __future__ import annotations

from typing import Any

from . import google, huggingface, openrouter
from .models import claude_model, compact_model_name, managed_model, search_models
from .registry import key_entry, list_accounts, list_keys, read_key

ANTHROPIC_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "claude-fable-5-1",
        "name": "Claude Fable 5.1",
        "description": "Most capable Claude model for long-running agents",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-opus-5",
        "name": "Claude Opus 5",
        "description": "Latest flagship Claude model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-sonnet-5",
        "name": "Claude Sonnet 5",
        "description": "Latest balanced Claude model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-fable-5",
        "name": "Claude Fable 5",
        "description": "Fast agentic Claude 5 model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-opus-4-8",
        "name": "Claude Opus 4.8",
        "description": "High-capability Claude model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-opus-4-7",
        "name": "Claude Opus 4.7",
        "description": "High-capability Claude model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-opus-4-6",
        "name": "Claude Opus 4.6",
        "description": "Most capable Claude model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "description": "Balanced Claude coding model",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "name": "Claude Haiku 4.5",
        "description": "Fast Claude model",
        "context_length": 200_000,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    },
)


def _decorate(
    models: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    provider: str,
    credential: str,
    credential_label: str | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in models:
        model = dict(source)
        model["provider"] = provider
        model["credential"] = credential
        if credential_label:
            model["credential_label"] = credential_label
        model["name"] = compact_model_name(model)
        model["route"] = managed_model(model)
        result.append(model)
    return result


def account_models() -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for account in list_accounts():
        label = str(account.get("label") or account["id"])
        account_catalog = _decorate(
            ANTHROPIC_MODELS,
            "anthropic",
            str(account["id"]),
            label,
        )
        for field in ("subscription", "rate_limit_tier"):
            value = account.get(field)
            if isinstance(value, str) and value:
                for model in account_catalog:
                    model[field] = value
        models.extend(account_catalog)
    return models


def anthropic_api_models() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in list_keys("anthropic-api"):
        result.extend(
            _decorate(
                ANTHROPIC_MODELS,
                "anthropic-api",
                str(entry["id"]),
                str(entry.get("label") or entry["id"]),
            )
        )
    return result


def refresh_key_catalog(key_id: str) -> list[dict[str, Any]]:
    entry = key_entry(key_id)
    provider = str(entry["provider"])
    key = read_key(key_id, provider=provider)
    if provider == "openrouter":
        models = openrouter.refresh_catalog(key, key_id)
    elif provider == "google":
        models = google.refresh_catalog(key_id, key)
    elif provider == "huggingface":
        models = huggingface.refresh_catalog(key_id, key)
    elif provider == "anthropic-api":
        models = list(ANTHROPIC_MODELS)
    else:  # registry validation should make this unreachable
        raise RuntimeError(f"unsupported provider: {provider}")
    return _decorate(models, provider, key_id, str(entry.get("label") or key_id))


def load_key_catalog(key_id: str) -> list[dict[str, Any]]:
    entry = key_entry(key_id)
    provider = str(entry["provider"])
    if provider == "openrouter":
        models = openrouter.load_catalog(key_id)
    elif provider == "google":
        models = google.load_catalog(key_id)
    elif provider == "huggingface":
        models = huggingface.load_catalog(key_id)
    elif provider == "anthropic-api":
        models = list(ANTHROPIC_MODELS)
    else:
        raise RuntimeError(f"unsupported provider: {provider}")
    return _decorate(models, provider, key_id, str(entry.get("label") or key_id))


def refresh_all_catalogs() -> list[dict[str, Any]]:
    models = account_models() + anthropic_api_models()
    for entry in list_keys():
        if entry.get("provider") in {"openrouter", "google", "huggingface"}:
            models.extend(refresh_key_catalog(str(entry["id"])))
    return models


def refresh_select_catalogs() -> list[dict[str, Any]]:
    """Refresh key-filtered OpenRouter catalogs and load every other route.

    Selection must not offer stale OpenRouter routes because the effective
    model allowlist can change when a key's guardrails change. Other provider
    catalogs retain their existing explicit refresh behavior.
    """
    models = account_models() + anthropic_api_models()
    for entry in list_keys():
        key_id = str(entry["id"])
        if entry.get("provider") in {"openrouter", "huggingface"}:
            models.extend(refresh_key_catalog(key_id))
        elif entry.get("provider") == "google":
            models.extend(load_key_catalog(key_id))
    return models


def load_all_catalogs(*, tolerate_missing: bool = False) -> list[dict[str, Any]]:
    models = account_models() + anthropic_api_models()
    for entry in list_keys():
        if entry.get("provider") not in {"openrouter", "google", "huggingface"}:
            continue
        try:
            models.extend(load_key_catalog(str(entry["id"])))
        except RuntimeError:
            if not tolerate_missing:
                raise
    return models


def exact_routes(models: list[dict[str, Any]], requested: list[str]) -> list[dict[str, Any]]:
    """Resolve exact managed ids or convenient provider/credential/model specs."""
    by_route = {managed_model(model): model for model in models}
    by_route.update({claude_model(model): model for model in models})
    by_spec = {
        f"{model['provider']}/{model['credential']}/{model['id']}": model for model in models
    }
    by_plain: dict[str, list[dict[str, Any]]] = {}
    for model in models:
        by_plain.setdefault(str(model["id"]), []).append(model)
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    for request in requested:
        model = by_route.get(request) or by_spec.get(request)
        if model is None:
            candidates = by_plain.get(request, [])
            if len(candidates) == 1:
                model = candidates[0]
            elif len(candidates) > 1:
                choices = ", ".join(
                    f"{item['provider']}/{item['credential']}/{item['id']}" for item in candidates
                )
                raise ValueError(f"model {request} is ambiguous; choose one of: {choices}")
        if model is None:
            missing.append(request)
        elif managed_model(model) not in {managed_model(item) for item in selected}:
            selected.append(model)
    if missing:
        raise ValueError(f"model route not found in the current indexes: {', '.join(missing)}")
    if not selected:
        raise ValueError("select at least one model")
    return selected


def search_all(models: list[dict[str, Any]], queries: list[str]) -> list[dict[str, Any]]:
    """Search regular fields plus provider and credential labels."""
    augmented: list[dict[str, Any]] = []
    for model in models:
        copy = dict(model)
        extra = " ".join(
            str(value)
            for value in (
                model.get("description"),
                model.get("provider"),
                model.get("credential"),
                model.get("credential_label"),
            )
            if value
        )
        copy["description"] = extra
        augmented.append(copy)
    found = search_models(augmented, queries)
    routes = {managed_model(model) for model in found}
    return [model for model in models if managed_model(model) in routes]
