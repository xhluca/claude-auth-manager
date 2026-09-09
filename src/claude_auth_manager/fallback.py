"""Explicit fallback links, failure classification, and runtime cooldowns."""

from __future__ import annotations

import math
import threading
import time
from contextlib import suppress
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .models import managed_model
from .paths import state_dir
from .storage import atomic_write_json, read_json_object


def state_path() -> Path:
    return state_dir() / "fallback-state.json"


def validate_links(links: Any, routes: set[str]) -> dict[str, str]:
    if not isinstance(links, dict):
        raise ValueError("fallbacks must be a mapping of source routes to target routes")
    for source, target in links.items():
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("fallback links must contain route strings")
        if source not in routes or target not in routes:
            raise ValueError("fallback source and target must both be selected favorites")
        visited = {source}
        cursor = target
        while True:
            if cursor in visited:
                raise ValueError("fallback links cannot contain a cycle or point to themselves")
            visited.add(cursor)
            if cursor not in links:
                break
            cursor = links[cursor]
    return dict(links)


def selected_links(document: dict[str, Any]) -> dict[str, str]:
    routes = {
        managed_model(model) for model in document.get("favorites", []) if isinstance(model, dict)
    }
    return validate_links(document.get("fallbacks", {}), routes)


def failure_kind(status: int, payload: Any = None) -> str | None:
    """Use structured codes, never arbitrary message text to bypass a rejection."""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {}
    # Explicit HTTP rejection wins over nested, possibly unrelated metadata.
    if status in {400, 401, 403, 404, 413, 422}:
        return None
    if status == 402:
        return "credits exhausted"
    if status == 429:
        return "usage limit"
    if status in {408, 500, 502, 503, 504, 529}:
        return "provider unavailable"
    if status >= 400:
        return None
    code = error.get("code")
    if isinstance(code, int):
        return failure_kind(code)
    kind = error.get("type") or error.get("status")
    if kind in {"rate_limit_error", "RESOURCE_EXHAUSTED", "insufficient_quota"}:
        return "usage limit"
    if kind in {"overloaded_error", "api_error", "timeout_error", "UNAVAILABLE", "INTERNAL"}:
        return "provider unavailable"
    return None


def retry_delay(kind: str, headers: dict[str, str], now: float) -> float:
    """Honor server deadlines; use 60s outages, 5m limits, 15m credits otherwise."""
    values: list[float] = []
    headers = {key.casefold(): value for key, value in headers.items()}
    retry = headers.get("retry-after", "")
    if retry:
        try:
            values.append(float(retry))
        except ValueError:
            with suppress(ValueError, TypeError, OverflowError):
                values.append(parsedate_to_datetime(retry).timestamp() - now)
    # Unified subscription limits report epoch seconds. API limits report ISO times.
    for key, value in headers.items():
        if key.startswith("anthropic-ratelimit-") and key.endswith("-reset"):
            status = headers.get(key.removesuffix("-reset") + "-status")
            remaining = headers.get(key.removesuffix("-reset") + "-remaining")
            if status != "rejected" and remaining != "0":
                continue
            try:
                deadline = float(value)
            except ValueError:
                try:
                    deadline = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
            values.append(deadline - now)
    valid = [value for value in values if math.isfinite(value) and value > 0]
    return (
        max(valid)
        if valid
        else {
            "provider unavailable": 60.0,
            "usage limit": 300.0,
            "credits exhausted": 900.0,
        }[kind]
    )


class UpstreamFailure(Exception):
    def __init__(self, kind: str, status: int = 503, headers: dict[str, str] | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.headers = headers or {}


class FallbackState:
    """Thread-safe route-local circuit state; persisted without provider error bodies."""

    def __init__(self, *, persistent: bool = False, clock: Any = time.time):
        self.lock = threading.RLock()
        self.clock = clock
        self.persistent = persistent
        self.failures: dict[str, dict[str, Any]] = {}
        self.active: dict[str, dict[str, Any]] = {}
        if persistent:
            document = read_json_object(state_path(), missing_ok=True)
            for route, value in document.get("failures", {}).items():
                if isinstance(value, dict) and isinstance(value.get("until"), (float, int)):
                    self.failures[route] = value
            self.active = document.get("active", {})

    def _save(self) -> None:
        if self.persistent:
            # A display/state disk failure must not replay a successful provider request.
            with suppress(OSError):
                atomic_write_json(
                    state_path(), {"version": 1, "failures": self.failures, "active": self.active}
                )

    def unavailable(self, route: str) -> dict[str, Any] | None:
        with self.lock:
            failure = self.failures.get(route)
            return dict(failure) if failure and failure["until"] > self.clock() else None

    def failed(self, route: str, error: UpstreamFailure) -> None:
        with self.lock:
            now = self.clock()
            self.failures[route] = {
                "reason": error.kind,
                "until": now + retry_delay(error.kind, error.headers, now),
            }
            self._save()

    def succeeded(self, source: str, target: str, reason: str) -> bool:
        with self.lock:
            recovered = self.failures.pop(target, None)
            previous = self.active.get(source)
            current = {"target": target, "reason": reason} if source != target else None
            if current:
                self.active[source] = current
            else:
                self.active.pop(source, None)
            if recovered or previous != current:
                self._save()
            return previous != current

    def exhausted(self, source: str, reason: str) -> None:
        with self.lock:
            self.active[source] = {"exhausted": True, "reason": reason}
            self._save()
