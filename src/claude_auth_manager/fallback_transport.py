"""Retry managed requests before committing output; preserve streaming/tool semantics."""

from __future__ import annotations

import http.client
import itertools
import json
import ssl
from collections.abc import Iterable, Iterator
from contextlib import suppress
from typing import Any

from .fallback import UpstreamFailure, failure_kind, fallback_order, selected_links
from .google import (
    OpenAIStreamTranslator,
    anthropic_to_openai,
    approximate_input_tokens,
    openai_to_anthropic,
)
from .models import compact_model_name, picker_source


def encode_event(payload: dict[str, Any]) -> bytes:
    return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def events(response: Any) -> Iterator[dict[str, Any]]:
    data: list[bytes] = []
    size = 0
    while True:
        try:
            raw = response.readline(1024 * 1024 + 1)
        except OSError as exc:
            # Distinguish an upstream reset from the client closing our output pipe.
            raise UpstreamFailure("provider unavailable", 502) from exc
        if not raw:
            if data:
                yield json.loads(b"\n".join(data))
            return
        size += len(raw)
        if size > 1024 * 1024:
            raise UpstreamFailure("provider unavailable", 502)
        line = raw.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data.append(line[5:].lstrip())
        elif not line:
            size = 0
            if data:
                encoded = b"\n".join(data)
                data.clear()
                if encoded == b"[DONE]":
                    return
                payload = json.loads(encoded)
                if not isinstance(payload, dict):
                    raise UpstreamFailure("provider unavailable", 502)
                yield payload


def anthropic_events(response: Any, provider: str, model: str, metadata: dict) -> Iterator[dict]:
    translator = (
        OpenAIStreamTranslator(model, tool_metadata=metadata)
        if provider in {"google", "huggingface"}
        else None
    )
    stopped = False
    for payload in events(response):
        if payload.get("error") or payload.get("type") == "error":
            kind = failure_kind(200, payload)
            if kind:
                raise UpstreamFailure(kind, 503, dict(response.getheaders()))
            yield (
                payload
                if payload.get("type") == "error"
                else {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Upstream rejected the stream",
                    },
                }
            )
            return
        if translator:
            for chunk in translator.feed(payload):
                yield json.loads(chunk.split(b"data: ", 1)[1])
        else:
            yield payload
            if payload.get("type") == "message_stop":
                stopped = True
                return
    if translator and translator.finish_reason:
        for chunk in translator.finish():
            yield json.loads(chunk.split(b"data: ", 1)[1])
    elif not stopped:
        raise UpstreamFailure("provider unavailable", 502)


def preflight(stream: Iterator[dict]) -> tuple[list[dict], Iterator[dict]]:
    """Hold headers/start/pings until the first content; early SSE errors can retry."""
    pending: list[dict] = []
    size = 0
    for event in stream:
        pending.append(event)
        size += len(json.dumps(event))
        kind = event.get("type")
        block = event.get("content_block", {})
        delta = event.get("delta", {})
        has_content = (
            kind == "content_block_start"
            and (
                block.get("type") not in {"text", "thinking"}
                or block.get("text")
                or block.get("thinking")
            )
        ) or (
            kind == "content_block_delta"
            and any(delta.get(key) for key in ("text", "thinking", "partial_json", "signature"))
        )
        if has_content or kind in {"message_stop", "error"}:
            return pending, stream
        if size > 1024 * 1024:
            raise UpstreamFailure("provider unavailable", 502)
    raise UpstreamFailure("provider unavailable", 502)


def announced_events(stream: Iterable[dict], notice: str) -> Iterator[dict]:
    for event in stream:
        event = dict(event)
        if notice and isinstance(event.get("index"), int):
            event["index"] += 1
        yield event
        if notice and event.get("type") == "message_start":
            yield {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
            yield {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": notice + "\n\n"},
            }
            yield {"type": "content_block_stop", "index": 0}


def route_label(route: str, routes: dict) -> str:
    model = routes[route]
    return f"{compact_model_name(model)} — {picker_source(model)}"


def _response_headers(handler: Any, response: Any) -> None:
    from .proxy import HOP_BY_HOP

    for key, value in response.getheaders():
        if key.casefold() not in HOP_BY_HOP | {"content-type", "content-length", "server", "date"}:
            handler.send_header(key, value)


def _json(
    handler: Any,
    status: int,
    body: bytes,
    content_type: str = "application/json",
    response: Any = None,
) -> None:
    handler.send_response(status)
    if response is not None:
        _response_headers(handler, response)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler._response_started = True
    handler.wfile.write(body)


def _success(handler: Any, source: str, target: str, reason: str) -> None:
    state = handler.router.fallback_state
    with state.lock:
        changed = state.succeeded(source, target, reason)
        if changed and state.persistent:
            from .settings import refresh_fallback_picker

            with suppress(OSError, RuntimeError):
                refresh_fallback_picker(state.active)


def forward_managed(handler: Any, original: bytes) -> None:
    from . import classifier
    from .proxy import (
        RouteDecision,
        _append_system_notice,
        _filter_gemini_sse_event,
        _remove_gemini_thinking_content,
        _target,
        route_managed_payload,
    )
    from .settings import load_preferences

    router = handler.router
    initial, _payload, _body = route_managed_payload(original, router.routes)
    preferences = load_preferences()
    classifier_routes = (
        classifier.routes(initial.model, classifier.accounts(preferences))
        if initial.provider == "native" and classifier.matches(initial.model)
        else {}
    )
    source = initial.route_id
    links = selected_links(preferences) if router.fallbacks is None else dict(router.fallbacks)
    # Only configured routes authorize credential changes. Native built-ins stay native.
    source_model = router.routes.get(source)
    if source_model:
        from .models import managed_model

        source = managed_model(source_model)
    if classifier_routes:
        source = f"classifier/{initial.model}"
        # Separate account-only chain; never inherit arbitrary-model chat fallbacks.
        links = {source: list(classifier_routes)}
    candidates = iter(
        list(classifier_routes) if classifier_routes else fallback_order(source, links)
    )
    target = next(candidates, None)
    visited: set[str] = set()
    reason = ""
    last_error: UpstreamFailure | None = None
    path = handler.path.split("?", 1)[0].rstrip("/")
    count_only = path.endswith("/count_tokens")
    state = router.fallback_state
    while target and target not in visited:
        visited.add(target)
        if target != source and target not in router.routes and target not in classifier_routes:
            break
        unavailable = state.unavailable(target) if source in links else None
        if unavailable and not count_only:
            reason = unavailable["reason"]
            last_error = UpstreamFailure(reason, 429 if reason == "usage limit" else 503)
            target = next(candidates, None)
            continue
        payload = json.loads(original)
        if classifier_routes:
            # Preserve exact model, system prompt, structured output, thinking, and
            # response schema. A classifier must receive no chat routing banners.
            decision = RouteDecision("anthropic", classifier_routes[target], initial.model, target)
            body = original
        else:
            payload["model"] = target
            decision, payload, body = route_managed_payload(
                json.dumps(payload).encode(), router.routes
            )
        notice = ""
        if target != source and not classifier_routes:
            notice = (
                f"⚠ CAM fallback active: {route_label(source, router.routes)}\n"
                f"→ {route_label(target, router.routes)} ({reason})."
            )
            _append_system_notice(
                payload,
                "CAM selected this fallback because the requested route is unavailable. " + notice,
            )
            # Thinking signatures and provider controls are not portable across models.
            payload.pop("thinking", None)
            payload.pop("output_config", None)
            for message in payload.get("messages", []):
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = [
                        block
                        for block in content
                        if not isinstance(block, dict)
                        or block.get("type") not in {"thinking", "redacted_thinking"}
                    ]
            body = json.dumps(payload).encode()
        metadata = (
            router.google_metadata(decision.credential) if decision.provider == "google" else {}
        )
        if count_only and decision.provider in {"google", "huggingface"}:
            handler._json_response(200, {"input_tokens": approximate_input_tokens(payload)})
            return
        upstream = {
            "openrouter": router.openrouter_upstream,
            "google": router.google_upstream,
        }.get(decision.provider, router.anthropic_upstream)
        if decision.provider == "huggingface":
            from .huggingface import API_BASE, require_free_route
            from .registry import read_key

            require_free_route(
                decision.model, read_key(decision.credential, provider="huggingface")
            )
            upstream = API_BASE
        if decision.provider in {"google", "huggingface"}:
            body = json.dumps(anthropic_to_openai(payload, metadata)).encode()
        headers = handler._upstream_headers(
            decision.provider, decision.model, len(body), credential=decision.credential
        )
        connection_type, host, port, base = _target(upstream)
        kwargs: dict[str, Any] = {"timeout": router.fallback_timeout}
        if connection_type is http.client.HTTPSConnection:
            kwargs["context"] = ssl.create_default_context()
        connection = connection_type(host, port, **kwargs)
        try:
            endpoint = (
                "/chat/completions"
                if decision.provider in {"google", "huggingface"}
                else handler.path
            )
            connection.request("POST", base + endpoint, body=body, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "application/json")
            kind = failure_kind(response.status)
            if kind and not count_only:
                raise UpstreamFailure(kind, response.status, dict(response.getheaders()))
            if response.status >= 400:
                _json(handler, response.status, response.read(), content_type, response)
                return
            normalize = decision.provider == "openrouter" and decision.model.startswith(
                "google/gemini"
            )
            if "text/event-stream" in content_type:
                pending, stream = preflight(
                    iter(anthropic_events(response, decision.provider, decision.model, metadata))
                )
                if any(event.get("type") == "error" for event in pending):
                    _json(handler, 400, json.dumps(pending[-1]).encode())
                    return
                _success(handler, source, target, reason)
                handler.send_response(200)
                _response_headers(handler, response)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Transfer-Encoding", "chunked")
                handler.end_headers()
                handler._response_started = True
                if connection.sock:
                    connection.sock.settimeout(600)
                thinking_indexes: set[int] = set()
                for event in announced_events(itertools.chain(pending, stream), notice):
                    chunk = encode_event(event)
                    if normalize:
                        chunk = _filter_gemini_sse_event(chunk, thinking_indexes)
                    handler._write_chunk(chunk)
                handler.wfile.write(b"0\r\n\r\n")
                handler.wfile.flush()
            else:
                data = response.read()
                document = json.loads(data)
                kind = failure_kind(response.status, document)
                if kind and not count_only:
                    raise UpstreamFailure(kind, 503, dict(response.getheaders()))
                if isinstance(document, dict) and document.get("error"):
                    _json(handler, 400, data)
                    return
                if decision.provider in {"google", "huggingface"}:
                    document = openai_to_anthropic(document, decision.model, metadata)
                if notice and not count_only:
                    document["content"] = [
                        {"type": "text", "text": notice + "\n\n"},
                        *document.get("content", []),
                    ]
                data = json.dumps(document).encode()
                if normalize:
                    data = _remove_gemini_thinking_content(data)
                if not count_only:
                    _success(handler, source, target, reason)
                _json(handler, response.status, data, response=response)
            handler._record_status(
                decision.provider, decision.model, None, credential=decision.credential
            )
            return
        except (BrokenPipeError, ConnectionResetError):
            if handler._response_started:
                return  # Client cancellation does not cool down a healthy provider.
            last_error = UpstreamFailure("provider unavailable", 502)
            if not count_only:
                state.failed(target, last_error)
            reason = last_error.kind
            target = None if count_only else next(candidates, None)
        except (UpstreamFailure, OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            error = (
                exc
                if isinstance(exc, UpstreamFailure)
                else UpstreamFailure("provider unavailable", 502)
            )
            if not count_only:
                state.failed(target, error)
            if handler._response_started:
                # Never replay a response after text/tool output escaped to the client.
                # Claude's retry (or its next request) will use the now-cooled-down route.
                try:
                    handler._write_chunk(
                        encode_event(
                            {
                                "type": "error",
                                "error": {
                                    "type": "api_error",
                                    "message": (
                                        "CAM: upstream stream interrupted. The next retry uses "
                                        "the configured fallback, if available. Partial output "
                                        "was not replayed."
                                    ),
                                },
                            }
                        )
                    )
                    handler.wfile.write(b"0\r\n\r\n")
                    handler.wfile.flush()
                except OSError:
                    pass
                return
            last_error = error
            reason = error.kind
            target = None if count_only else next(candidates, None)
        finally:
            connection.close()
    status = last_error.status if last_error else 503
    if source in links and not count_only:
        with state.lock:
            state.exhausted(source, reason)
            if state.persistent:
                from .settings import refresh_fallback_picker

                with suppress(OSError, RuntimeError):
                    refresh_fallback_picker(state.active)
    handler._error_response(
        status,
        "rate_limit_error" if status == 429 else "api_error",
        "CAM: no available route in the configured fallback chain ("
        + (reason or "unavailable")
        + ").",
    )
