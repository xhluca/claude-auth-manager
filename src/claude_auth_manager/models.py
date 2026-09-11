"""Model matching, ranking, and display helpers."""

from __future__ import annotations

import fnmatch
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

MANAGED_MODEL_PREFIX = "cam/"
OPENROUTER_MODEL_PREFIX = "cam/openrouter/"
SUPPORTED_ROUTES = frozenset({"anthropic", "anthropic-api", "openrouter", "google", "huggingface"})


def supported_parameters(model: dict[str, Any]) -> frozenset[str] | None:
    """Return normalized OpenRouter parameters, or ``None`` when unknown."""
    values = model.get("supported_parameters")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return frozenset(value.casefold() for value in values)


def supports_parameter(model: dict[str, Any], parameter: str) -> bool | None:
    parameters = supported_parameters(model)
    return None if parameters is None else parameter.casefold() in parameters


def supports_tools(model: dict[str, Any]) -> bool:
    return supports_parameter(model, "tools") is True


def _capability_mark(value: bool | None) -> str:
    if value is True:
        return "✓"
    if value is False:
        return "✗"
    return "?"


def tool_capability_badge(model: dict[str, Any], *, detailed: bool = False) -> str:
    tools = _capability_mark(supports_parameter(model, "tools"))
    if not detailed:
        return f"tools {tools}"
    tool_choice = _capability_mark(supports_parameter(model, "tool_choice"))
    return f"tools {tools} · tool choice {tool_choice}"


def input_modalities(model: dict[str, Any]) -> frozenset[str] | None:
    """Return normalized catalog input modalities, or ``None`` when unknown."""
    architecture = model.get("architecture")
    if not isinstance(architecture, dict):
        return None
    values = architecture.get("input_modalities")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return frozenset(value.casefold() for value in values)


def catalog_input_modalities(
    models: list[dict[str, Any]],
) -> dict[str, frozenset[str]]:
    """Index known input capabilities by exact OpenRouter model id."""
    result: dict[str, frozenset[str]] = {}
    for model in models:
        model_id = model.get("id")
        modalities = input_modalities(model)
        if isinstance(model_id, str) and modalities is not None:
            result[model_id] = modalities
    return result


def namespaced_model(
    model_id: str,
    provider: str = "openrouter",
    credential: str | None = None,
) -> str:
    """Return a fail-closed picker model id.

    ``credential=None`` returns the provider-scoped legacy route form.
    New manager routes always include the credential/account component.
    """
    if credential is None:
        return f"{MANAGED_MODEL_PREFIX}{provider}/{model_id}"
    return f"{MANAGED_MODEL_PREFIX}{provider}/{credential}/{model_id}"


def managed_model(model: dict[str, Any]) -> str:
    provider = str(model.get("provider") or "openrouter")
    credential = model.get("credential")
    return namespaced_model(
        str(model["id"]),
        provider,
        str(credential) if isinstance(credential, str) else None,
    )


def uses_extended_context(model: dict[str, Any]) -> bool:
    """Identify Claude routes whose gateway 1M window needs explicit selection."""
    context = model.get("context_length")
    model_id = str(model.get("id") or "").removeprefix("anthropic/")
    return (
        model.get("provider", "openrouter") in {"anthropic", "anthropic-api", "openrouter"}
        and model_id.startswith(("claude-sonnet-", "claude-opus-", "claude-fable-"))
        and isinstance(context, int)
        and context >= 1_000_000
    )


def claude_model(model: dict[str, Any]) -> str:
    """Return the client selection, including Claude's context-window annotation.

    Claude strips [1m] before sending the request, so the router's credential-
    scoped allowlist and the upstream model ID remain unchanged.
    """
    route = managed_model(model)
    return f"{route}[1m]" if uses_extended_context(model) and not route.endswith("[1m]") else route


def parse_managed_model(model_id: str) -> tuple[str, str | None, str] | None:
    if not model_id.startswith(MANAGED_MODEL_PREFIX):
        return None
    remainder = model_id[len(MANAGED_MODEL_PREFIX) :]
    provider, separator, tail = remainder.partition("/")
    if not separator or provider not in SUPPORTED_ROUTES:
        return None
    # The legacy OpenRouter route has no credential component.
    if provider == "openrouter" and tail.count("/") == 1:
        return provider, None, tail
    credential, separator, upstream_model = tail.partition("/")
    if not separator or not credential or not upstream_model:
        return None
    return provider, credential, upstream_model


def original_model(model_id: str) -> str | None:
    parsed = parse_managed_model(model_id)
    if parsed is None or parsed[0] != "openrouter":
        return None
    return parsed[2]


def hybrid_openrouter_allowed(model_id: str) -> bool:
    normalized = model_id.casefold()
    return not normalized.startswith("anthropic/") and normalized != "openrouter/auto"


def searchable_text(model: dict[str, Any]) -> str:
    values = (model.get("id"), model.get("name"), model.get("description"))
    return "\n".join(value for value in values if isinstance(value, str))


def searchable_fields(model: dict[str, Any]) -> list[str]:
    values = (model.get("id"), model.get("name"), model.get("description"))
    return [value for value in values if isinstance(value, str)]


def _glob_pattern(query: str) -> str:
    return query if any(marker in query for marker in "*?[") else f"*{query}*"


def search_models(
    models: list[dict[str, Any]], queries: list[str], *, regex: bool = False
) -> list[dict[str, Any]]:
    if not queries:
        return list(models)
    if regex:
        try:
            patterns = [re.compile(query, re.IGNORECASE) for query in queries]
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc

        def matches(model: dict[str, Any]) -> bool:
            fields = searchable_fields(model)
            return any(
                pattern.search(field) is not None for field in fields for pattern in patterns
            )

    else:
        patterns = [_glob_pattern(query).casefold() for query in queries]

        def matches(model: dict[str, Any]) -> bool:
            fields = [field.casefold() for field in searchable_fields(model)]
            return any(
                fnmatch.fnmatchcase(field, pattern) for field in fields for pattern in patterns
            )

    found = [model for model in models if matches(model)]
    return sorted(found, key=lambda model: _rank(model, queries))


def _rank(model: dict[str, Any], queries: list[str]) -> tuple[int, int, str]:
    model_id = str(model.get("id", "")).casefold()
    name = str(model.get("name", "")).casefold()
    plain = [query.casefold().strip("*?") for query in queries]
    score = 50
    for query in plain:
        if not query:
            continue
        if model_id == query:
            score = min(score, 0)
        elif name == query:
            score = min(score, 1)
        elif model_id.endswith(f"/{query}"):
            score = min(score, 2)
        elif query in model_id:
            score = min(score, 4 + model_id.index(query))
        elif query in name:
            score = min(score, 6 + name.index(query))
    return score, len(model_id), model_id


def top_matches(
    models: list[dict[str, Any]], query: str, *, limit: int = 15
) -> list[dict[str, Any]]:
    if not query.strip():
        return models[:limit]
    return search_models(models, [query])[:limit]


def exact_models(models: list[dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    by_id = {str(model["id"]): model for model in models}
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()
    for model_id in ids:
        if model_id in seen:
            continue
        seen.add(model_id)
        model = by_id.get(model_id)
        if model is None:
            missing.append(model_id)
        else:
            selected.append(model)
    if missing:
        rendered = ", ".join(missing)
        raise ValueError(f"model not found in the current OpenRouter index: {rendered}")
    if not selected:
        raise ValueError("select at least one model")
    return selected


def _price_per_million(value: Any, *, decimal_places: int) -> str | None:
    try:
        price = Decimal(str(value)) * 1_000_000
        quantum = Decimal(1).scaleb(-decimal_places)
        rounded = price.quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    rendered = f"{rounded:f}".rstrip("0").rstrip(".")
    return rendered if rendered not in {"", "-0"} else "0"


_VENDOR_LABELS = {
    "ai21": "AI21",
    "alibaba": "Alibaba",
    "amazon": "Amazon",
    "anthropic": "Claude",
    "cohere": "Cohere",
    "deepseek": "DeepSeek",
    "google": "Google",
    "meta": "Meta",
    "meta-llama": "Meta",
    "microsoft": "Microsoft",
    "mistral": "Mistral AI",
    "mistralai": "Mistral AI",
    "moonshotai": "Moonshot AI",
    "muse": "Muse",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "perplexity": "Perplexity",
    "qwen": "Qwen",
    "x-ai": "xAI",
    "z-ai": "Z.AI",
}


def _humanize_vendor(value: str) -> str:
    known = _VENDOR_LABELS.get(value.casefold())
    if known:
        return known
    return " ".join(part.capitalize() for part in re.split(r"[-_]", value) if part)


def claude_subscription_label(value: object, rate_limit_tier: object = None) -> str:
    """Return the plan and its reported multiplier, without guessing missing tiers."""
    if not isinstance(value, str):
        return "Subscription"
    tier = value.strip()
    if not tier or tier.casefold() in {"subscription", "unknown", "none"}:
        return "Subscription"
    tier = re.sub(r"^claude\s+", "", tier, flags=re.IGNORECASE)
    if tier.casefold() == "max" and isinstance(rate_limit_tier, str):
        multiplier = re.fullmatch(
            r"(?:default_claude_)?max_([1-9][0-9]*)x", rate_limit_tier, re.IGNORECASE
        )
        if multiplier:
            return f"Max {multiplier[1]}x"
    return _humanize_vendor(tier)


def upstream_provider_label(model: dict[str, Any]) -> str:
    """Return the model maker, independently of its credential route."""
    provider = str(model.get("provider") or "openrouter")
    if provider in {"anthropic", "anthropic-api"}:
        return "Claude"
    if provider == "google":
        return "Google"
    model_id = str(model.get("id") or "")
    vendor, separator, _tail = model_id.partition("/")
    if separator and vendor:
        return _humanize_vendor(vendor)
    return _humanize_vendor(provider)


def compact_model_name(model: dict[str, Any]) -> str:
    """Return a picker name without a redundant catalog provider prefix."""
    model_id = str(model.get("id") or "")
    name = model.get("name")
    label = name.strip() if isinstance(name, str) and name.strip() else model_id
    prefix, separator, remainder = label.partition(":")
    vendor = upstream_provider_label(model)
    raw_vendor = model_id.partition("/")[0]
    accepted = {
        vendor.casefold(),
        raw_vendor.casefold(),
        raw_vendor.replace("-", " ").replace("_", " ").casefold(),
    }
    if vendor == "Claude":
        accepted.add("anthropic")
    if separator and remainder.strip() and prefix.strip().casefold() in accepted:
        label = remainder.strip()
    if vendor == "Claude":
        label = re.sub(r"^Claude\s+(?=\S)", "", label, flags=re.IGNORECASE)
    if (
        uses_extended_context(model)
        and model_id.removeprefix("anthropic/").startswith(("claude-sonnet-", "claude-opus-"))
        and not label.casefold().endswith("(1m context)")
    ):
        label = f"{label} (1M context)"
    return label


def _route_source(model: dict[str, Any]) -> str:
    provider = str(model.get("provider") or "openrouter")
    source = {
        "anthropic": claude_subscription_label(
            model.get("subscription"), model.get("rate_limit_tier")
        ),
        "anthropic-api": "Anthropic API",
        "google": "Gemini API",
        "huggingface": "Hugging Face",
        "openrouter": "OpenRouter",
    }.get(provider, _humanize_vendor(provider))
    credential = model.get("credential_label") or model.get("credential")
    if isinstance(credential, str) and credential:
        return f"{source} ({credential})"
    return source


def picker_source(model: dict[str, Any]) -> str:
    """Return the uniform, user-facing provenance for a selector row."""
    provider = str(model.get("provider") or "openrouter")
    parts = [
        upstream_provider_label(model),
        f"{_route_source(model)} via claude-auth-manager",
    ]
    pricing = model.get("pricing")
    if isinstance(pricing, dict):
        prompt = _price_per_million(pricing.get("prompt"), decimal_places=3)
        completion = _price_per_million(pricing.get("completion"), decimal_places=3)
        if prompt is not None and completion is not None:
            cached = _price_per_million(pricing.get("input_cache_read"), decimal_places=4) or "—"
            parts.append(f"{prompt} in / {completion} out / {cached} cached ($/M)")
    if provider == "openrouter":
        model_id = model.get("id")
        if isinstance(model_id, str) and model_id:
            parts.append(model_id)
    return " · ".join(parts)


def picker_description(model: dict[str, Any]) -> str:
    """Return the same provenance in every picker and save confirmation."""
    return picker_source(model)


def picker_row(model: dict[str, Any], *, hybrid: bool = False) -> dict[str, str]:
    model_id = str(model["id"])
    return {
        "model": claude_model(model) if hybrid else model_id,
        # Keep the visible row and active-model name compact. Subscription rows
        # identify the plan/account; API routes retain their catalog metadata.
        "label": compact_model_name(model),
        "description": picker_description(model),
    }


def compact_row(model: dict[str, Any]) -> str:
    model_id = str(model.get("id", ""))
    name = model.get("name")
    context = model.get("context_length")
    context_text = f"{context:,}" if isinstance(context, int) else "-"
    label = name if isinstance(name, str) else ""
    tools = _capability_mark(supports_parameter(model, "tools"))
    tool_choice = _capability_mark(supports_parameter(model, "tool_choice"))
    return f"{model_id}\t{label}\t{context_text}\t{tools}\t{tool_choice}"


def print_models(models: list[dict[str, Any]], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(models, indent=2, ensure_ascii=False))
        return
    print("MODEL\tNAME\tCONTEXT\tTOOLS\tTOOL_CHOICE")
    for model in models:
        print(compact_row(model))
