"""Hugging Face provider-pinned, zero-price chat inference catalogs."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .paths import catalog_path
from .storage import atomic_write_json, read_json_object

API_BASE = "https://router.huggingface.co/v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_key(key: str) -> None:
    request = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            if not isinstance(json.load(response), dict):
                raise RuntimeError("invalid Hugging Face identity response")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Hugging Face token rejected (HTTP {exc.code})") from None


def fetch_models(key: str) -> list[dict]:
    request = urllib.request.Request(
        API_BASE + "/models", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            document = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Hugging Face catalog rejected (HTTP {exc.code})") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "Hugging Face catalog unavailable; no paid routes will be attempted"
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise RuntimeError("invalid Hugging Face model catalog")
    return free_models(document["data"])


def free_models(data: list) -> list[dict]:
    result = {}
    for model in data:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        for provider in model.get("providers", []):
            if not isinstance(provider, dict) or provider.get("status") != "live":
                continue
            price = provider.get("pricing") or {}
            zero_price = all(
                type(price.get(k)) in {int, float} and price[k] == 0 for k in ("input", "output")
            )
            if not (provider.get("is_free") is True or zero_price):
                continue
            name = provider.get("provider")
            if not isinstance(name, str) or not name:
                continue
            model_id = model["id"] + ":" + name
            parameters = ["tools", "tool_choice"] if provider.get("supports_tools") is True else []
            result[model_id] = {
                "id": model_id,
                "name": model["id"].split("/", 1)[-1],
                "description": "Hugging Face zero-price provider route",
                "architecture": model.get("architecture", {}),
                "context_length": provider.get("context_length"),
                "supported_parameters": parameters,
                "pricing": {"prompt": "0", "completion": "0"},
            }
    return list(result.values())


def refresh_catalog(key_id: str, key: str) -> list[dict]:
    models = fetch_models(key)
    atomic_write_json(
        catalog_path("huggingface", key_id),
        {
            "models": models,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return models


def load_catalog(key_id: str) -> list[dict]:
    document = read_json_object(catalog_path("huggingface", key_id), missing_ok=True)
    return document.get("models", [])


def require_free_route(model_id: str, key: str) -> None:
    if not any(model["id"] == model_id for model in fetch_models(key)):
        raise ValueError(
            "Hugging Face route is not currently advertised as free; refresh with cam index"
        )
