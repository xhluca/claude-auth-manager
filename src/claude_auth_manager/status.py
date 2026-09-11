"""Non-billable, credential-scoped provider status checks. Never return raw responses."""

from __future__ import annotations

import http.client
import json
import math
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from . import __version__, google, openrouter
from .registry import list_accounts, list_keys, read_account_token, read_key

ANTHROPIC_BASE = "https://api.anthropic.com"
WINDOWS = ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus", "seven_day_oauth_apps")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a credential to a redirect destination.


def _get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"claude-auth-manager/{__version__}",
            **headers,
        },
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("oversized response")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("invalid status response")
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _reset(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _usage(provider: str, document: dict) -> tuple[dict, bool | None, str]:
    if provider == "huggingface":
        return (
            {},
            None,
            "token accepted; inference permission and free-route availability checked on use",
        )
    if provider == "anthropic":
        windows = {}
        for name in WINDOWS:
            window = document.get(name)
            if (
                isinstance(window, dict)
                and (used := _number(window.get("utilization"))) is not None
            ):
                windows[name] = {"used_percent": used, "resets_at": _reset(window.get("resets_at"))}
        available = all(w["used_percent"] < 100 for w in windows.values()) if windows else None
        detail = "usage available" if windows else "credential accepted; quota unknown"
        if available is False:
            detail = "one or more usage windows reached their limit"
        return {"windows": windows}, available, detail
    if provider == "openrouter":
        data = document.get("data")
        if not isinstance(data, dict):
            raise ValueError("invalid key metadata")
        usage = {
            key: _number(data.get(key))
            for key in (
                "limit",
                "limit_remaining",
                "usage",
                "usage_daily",
                "usage_weekly",
                "usage_monthly",
            )
        }
        reset = data.get("limit_reset")
        usage["limit_reset"] = reset if reset in {"daily", "weekly", "monthly"} else None
        remaining = usage["limit_remaining"]
        # Remaining key budget is not the same as the account's credit balance.
        detail = "key valid; account credit balance not checked"
        if remaining is not None and remaining <= 0:
            return usage, False, "key spending limit reached"
        return usage, None, detail
    if not isinstance(document.get("data"), list):
        raise ValueError("invalid model response")
    return {}, None, "credential accepted; quota unknown"


def _check(entry: dict) -> dict:
    row = {
        "type": entry["type"],
        "id": entry["id"],
        "label": entry.get("label") or entry["id"],
        "provider": entry["provider"],
        "credential_ready": False,
        "status": "local_error",
        "http_status": None,
        "quota_available": None,
        "usage": {},
        "detail": "credential unavailable, expired, or login identity changed; re-add it",
    }
    provider = entry["provider"]
    try:
        token = (
            read_account_token(entry["id"])
            if entry["type"] == "account"
            else read_key(entry["id"], provider=provider)
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return row
    row["credential_ready"] = True
    headers = {"Authorization": f"Bearer {token}"}
    if provider == "anthropic":
        url = ANTHROPIC_BASE + "/api/oauth/usage"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    elif provider == "openrouter":
        url = openrouter.api_base() + "/key"
    elif provider == "google":
        url = google.api_base() + "/models"
    elif provider == "huggingface":
        url = "https://huggingface.co/api/whoami-v2"
    else:
        url = ANTHROPIC_BASE + "/v1/models?limit=1"
        headers = {"x-api-key": token, "anthropic-version": "2023-06-01"}
    try:
        document = _get(url, headers)
        row["http_status"] = 200
        usage, available, detail = _usage(provider, document)
        row.update(
            usage=usage,
            quota_available=available,
            detail=detail,
            status="limited" if available is False else "valid",
        )
    except urllib.error.HTTPError as exc:
        row["http_status"] = exc.code
        row["status"], row["detail"] = {
            401: ("unauthorized", "credential rejected; renew the login or replace the key"),
            402: ("limited", "provider reports insufficient credit"),
            403: (
                "forbidden",
                "status endpoint denied; permissions or token scope may be insufficient",
            ),
            429: ("rate_limited", "status endpoint rate-limited; retry later"),
        }.get(exc.code, ("unavailable", f"status endpoint returned HTTP {exc.code}"))
        exc.close()
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        row.update(status="unavailable", detail="status endpoint could not be reached")
    except (ValueError, TypeError):
        row.update(status="invalid_response", detail="provider returned invalid status metadata")
    return row


def check_credentials(*, account: str | None = None, key: str | None = None) -> dict:
    entries = []
    if key is None:
        entries.extend(dict(item, type="account", provider="anthropic") for item in list_accounts())
    if account is None:
        entries.extend(dict(item, type="key") for item in list_keys())
    query = account or key
    if query:
        entries = [
            e
            for e in entries
            if query.casefold()
            in {
                str(e["id"]).casefold(),
                str(e.get("label", "")).casefold(),
            }
        ]
        if not entries:
            raise ValueError("no saved credential matches that account/key")
        if len(entries) > 1:
            raise ValueError("ambiguous credential label; specify its exact ID")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(_check, entries))
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "billable": False,
        "credentials": rows,
        "passed": all(row["status"] == "valid" for row in rows),
    }


def usage_summary(row: dict) -> str:
    usage = row["usage"]
    if row["provider"] == "anthropic":
        names = {
            "five_hour": "5h",
            "seven_day": "7d",
            "seven_day_sonnet": "Sonnet 7d",
            "seven_day_opus": "Opus 7d",
            "seven_day_oauth_apps": "OAuth 7d",
        }
        return (
            "; ".join(
                f"{names[k]} {v['used_percent']:g}% used"
                for k, v in usage.get("windows", {}).items()
            )
            or row["detail"]
        )
    if row["provider"] == "openrouter" and usage.get("limit_remaining") is not None:
        return f"${usage['limit_remaining']:.3f} key budget left (account balance unknown)"
    return row["detail"]
