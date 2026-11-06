"""Live Claude subscription catalogs with an official public-table fallback."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .paths import catalog_path
from .registry import read_account_token
from .storage import atomic_write_json, read_json_object

MODELS_URL = "https://api.anthropic.com/v1/models"
PUBLIC_URL = "https://platform.claude.com/docs/en/models/overview.md"
MODEL_ID = re.compile(r"claude-[a-z][a-z0-9]*(?:-[a-z0-9]+)+\Z")
MAX_BYTES = 2 * 1024 * 1024


def cache_path(account_id: str):
    # Private account identifiers must not become diagnostic/cache labels.
    return catalog_path("anthropic", hashlib.sha256(account_id.encode()).hexdigest())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "claude-auth-manager", **headers})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            data = response.read(MAX_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        # Never include provider bodies, credentials, or account identities in errors.
        raise RuntimeError("Claude model discovery unavailable") from exc
    if len(data) > MAX_BYTES:
        raise ValueError("Claude model discovery response too large")
    return data


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and 0 < value <= 100_000_000 else None


def _model(model_id: str, name: Any = None, context: Any = None) -> dict:
    model = {
        "id": model_id,
        "name": name if isinstance(name, str) and name.strip() else model_id,
        "supported_parameters": ["tools", "tool_choice", "images"],
        "architecture": {"input_modalities": ["text", "image"]},
    }
    if (size := _positive_int(context)) is not None:
        model["context_length"] = size
    return model


def fetch_account_models(account_id: str) -> list[dict]:
    token = read_account_token(account_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
    }
    models, cursors = {}, set()
    cursor = None
    for _ in range(20):
        query = {"limit": "1000"}
        if cursor:
            query["after_id"] = cursor
        document = json.loads(_get(MODELS_URL + "?" + urllib.parse.urlencode(query), headers))
        if not isinstance(document, dict) or not isinstance(document.get("data"), list):
            raise ValueError("invalid Claude model discovery response")
        for item in document["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            model_id = item["id"]
            if not MODEL_ID.fullmatch(model_id) or item.get("lifecycle") == "retired":
                continue
            models[model_id] = _model(
                model_id, item.get("display_name"), item.get("max_input_tokens")
            )
        if not document.get("has_more"):
            if not models:
                raise ValueError("Claude returned no usable models")
            return list(models.values())
        cursor = document.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("invalid Claude model discovery pagination")
        cursors.add(cursor)
    raise ValueError("Claude model discovery pagination limit reached")


def parse_public_models(markdown: str) -> list[dict]:
    """Read only columns explicitly labelled Claude API ID in official tables."""
    models = {}
    rows: list[list[str]] = []
    for line in markdown.splitlines() + [""]:
        if line.strip().startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
            continue
        if not rows:
            continue
        ids = next((row for row in rows if row[0] == "Claude API ID"), None)
        contexts = next((row for row in rows if "Context window" in row[0]), [])
        if ids:
            for index, cell in enumerate(ids[1:], 1):
                match = re.fullmatch(r"`(claude-[a-z0-9-]+)`", cell)
                if not match or not MODEL_ID.fullmatch(match[1]):
                    continue
                model_id = match[1]
                label = rows[0][index] if index < len(rows[0]) else model_id
                if not re.fullmatch(r"Claude [A-Za-z]+ \d+(?:\.\d+)*", label):
                    label = model_id
                context = None
                if index < len(contexts):
                    amount = re.search(r"\b(\d+(?:\.\d+)?)\s*([MK])\s+tokens", contexts[index])
                    if amount:
                        context = int(float(amount[1]) * (1_000_000 if amount[2] == "M" else 1000))
                models[model_id] = _model(model_id, label, context)
        rows = []
    if not models:
        raise ValueError("official Claude model table could not be parsed")
    return list(models.values())


def fetch_public_models() -> list[dict]:
    return parse_public_models(_get(PUBLIC_URL, {}).decode("utf-8"))


def load_catalog(account_id: str) -> list[dict]:
    document = read_json_object(cache_path(account_id), missing_ok=True)
    values = document.get("models", [])
    if not isinstance(values, list):
        raise ValueError("invalid cached Claude model catalog")
    return [
        item
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and MODEL_ID.fullmatch(item["id"])
    ]


def refresh_catalog(account_id: str, public: dict, bundled: tuple) -> tuple[list[dict], str]:
    """Share a public lookup across accounts; preserve the last good cache on failure."""
    source = MODELS_URL
    try:
        models = fetch_account_models(account_id)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        if "models" not in public:
            try:
                public["models"] = fetch_public_models()
            except (OSError, RuntimeError, ValueError):
                public["models"] = []
        models = public["models"]
        source = PUBLIC_URL
        if not models:
            return load_catalog(account_id) or list(bundled), "cached/bundled"
        # Public overview tables feature the latest lineup, not every still-usable
        # older snapshot. Retain known entries so refresh doesn't erase favorites.
        known = {
            item["id"]: dict(item, catalog_source="cached")
            for item in [*bundled, *load_catalog(account_id)]
        }
        for item in models:
            known[item["id"]] = dict(item, catalog_source="public")
        models = [known[item["id"]] for item in models] + [
            item for key, item in known.items() if key not in {m["id"] for m in models}
        ]
    # A public table is not evidence of subscription entitlement.
    provenance = "account" if source == MODELS_URL else "public"
    models = [
        dict(model, catalog_source=model.get("catalog_source", provenance)) for model in models
    ]
    atomic_write_json(
        cache_path(account_id),
        {
            "source": source,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "models": models,
        },
    )
    return models, provenance
