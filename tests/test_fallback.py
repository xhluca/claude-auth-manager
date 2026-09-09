from __future__ import annotations

import http.client
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_auth_manager import cli
from claude_auth_manager.fallback import (
    FallbackState,
    retry_delay,
    state_path,
    validate_links,
)
from claude_auth_manager.fallback_transport import encode_event, events
from claude_auth_manager.models import managed_model
from claude_auth_manager.paths import claude_settings_path
from claude_auth_manager.proxy import LOCAL_TOKEN_HEADER, HybridRouterServer
from claude_auth_manager.registry import add_account_token, add_key
from claude_auth_manager.settings import configure_claude, load_preferences, save_fallbacks


class FaultProvider(BaseHTTPRequestHandler):
    """An external HTTP upstream controlled by tests; no router fault-injection backdoor."""

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        credential = self.headers.get("Authorization", "").removeprefix("Bearer ")
        with self.server.lock:
            self.server.calls.append((credential, payload, dict(self.headers)))
        mode = self.server.faults.get(credential, 200)
        if mode == "timeout":
            time.sleep(0.15)
            self.close_connection = True
            return
        if mode == "disconnect":
            self.close_connection = True
            return
        error = {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": "synthetic limit"},
        }
        if isinstance(mode, int) and mode >= 400:
            self.reply(mode, json.dumps(error).encode())
            return
        if mode == "json-error":
            self.reply(200, json.dumps(error).encode())
            return
        start = {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": payload["model"],
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 2, "output_tokens": 0},
            },
        }
        stream = [start]
        completed = any(
            block.get("type") == "tool_result"
            for message in payload.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict)
        )
        if mode == "empty-block-error":
            stream.append(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        if mode not in {"early-error", "empty-block-error"}:
            block = {"type": "tool_use", "id": "call_unique", "name": "Glob", "input": {}}
            stream += [
                {"type": "content_block_start", "index": 0, "content_block": block},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"pattern":"*.txt"}'},
                },
                {"type": "content_block_stop", "index": 0},
            ]
            if completed:
                stream = [
                    start,
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "CAM_JOB_CONTINUED_OK"},
                    },
                    {"type": "content_block_stop", "index": 0},
                ]
        if mode in {"early-error", "empty-block-error", "late-error"}:
            stream.append(error)
        else:
            stream += [
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "end_turn" if completed else "tool_use",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 3},
                },
                {"type": "message_stop"},
            ]
        if payload.get("stream"):
            if self.path.endswith("/chat/completions"):
                body = (
                    b"data: "
                    + json.dumps(
                        {
                            "id": "google_test",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "GOOGLE_OK"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ).encode()
                    + b"\n\ndata: [DONE]\n\n"
                )
            else:
                body = b"".join(encode_event(event) for event in stream)
            self.reply(200, body, "text/event-stream")
        elif self.path.endswith("/chat/completions"):
            self.reply(
                200,
                json.dumps(
                    {"choices": [{"message": {"content": "GOOGLE_OK"}, "finish_reason": "stop"}]}
                ).encode(),
            )
        else:
            content = [{"type": "text", "text": "UPSTREAM_OK"}]
            stop_reason = "end_turn"
            if payload.get("tools"):
                content = (
                    [{"type": "text", "text": "CAM_JOB_CONTINUED_OK"}]
                    if completed
                    else [
                        {
                            "type": "tool_use",
                            "id": "call_unique",
                            "name": "Glob",
                            "input": {"pattern": "*.txt"},
                        }
                    ]
                )
                stop_reason = "end_turn" if completed else "tool_use"
            self.reply(
                200,
                json.dumps(
                    {
                        **start["message"],
                        "content": content,
                        "stop_reason": stop_reason,
                    }
                ).encode(),
            )

    def reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "10")
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def chain(isolated_home):
    specs = [
        ("anthropic", "max", "claude-opus-5"),
        ("anthropic", "personal", "claude-sonnet-5"),
        ("openrouter", "router", "z-ai/glm-5.3-flash"),
        ("google", "google", "gemini-test"),
        ("openrouter", "backup", "deepseek/test"),
    ]
    models = []
    for provider, credential, model in specs:
        if provider == "anthropic":
            add_account_token(credential, "test-provider-secret-" + credential)
        else:
            add_key(provider, credential, "test-provider-secret-" + credential)
        models.append({"provider": provider, "credential": credential, "id": model, "name": model})
    routes = {managed_model(model): model for model in models}
    ids = list(routes)
    links = dict(zip(ids[:-1], ids[1:], strict=True))
    configure_claude(models, native_login=False)
    save_fallbacks(links)
    now = [1000.0]
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FaultProvider)
    upstream.faults = {}
    upstream.calls = []
    upstream.lock = threading.Lock()
    url = f"http://127.0.0.1:{upstream.server_port}"
    router = HybridRouterServer(
        ("127.0.0.1", 0),
        local_token="local-test",
        routes=routes,
        anthropic_upstream=url,
        openrouter_upstream=url,
        google_upstream=url,
        record_status=False,
        fallback_state=FallbackState(persistent=True, clock=lambda: now[0]),
    )
    with running(upstream), running(router):
        yield router, upstream, ids, now


def request(router, route, stream=False, **extra):
    connection = http.client.HTTPConnection("127.0.0.1", router.server_port, timeout=5)
    body = {
        "model": route,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 16,
        "stream": stream,
        **extra,
    }
    connection.request(
        "POST",
        "/v1/messages",
        json.dumps(body),
        headers={LOCAL_TOKEN_HEADER: "local-test", "X-Claude-Code-Session-Id": "unchanged-session"},
    )
    response = connection.getresponse()
    status = response.status
    raw = response.read()
    connection.close()
    return status, list(events(io.BytesIO(raw))) if stream and status == 200 else json.loads(raw)


@pytest.mark.parametrize(
    "status", [402, 408, 429, 500, 502, 503, 504, 529, "disconnect", "json-error"]
)
def test_error_falls_through_without_restarting_session(chain, status):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-max"] = status
    code, body = request(router, ids[0])
    assert code == 200
    assert body["model"] == "claude-sonnet-5"
    assert "CAM fallback active" in body["content"][0]["text"]
    assert [call[0] for call in upstream.calls] == [
        "test-provider-secret-max",
        "test-provider-secret-personal",
    ]
    assert all(
        call[2]["X-Claude-Code-Session-Id"] == "unchanged-session" for call in upstream.calls
    )


def test_five_accounts_chain_cooldown_recovery_and_picker(chain):
    router, upstream, ids, now = chain
    for key, status in [("max", 429), ("personal", 529), ("router", 402), ("google", 503)]:
        upstream.faults["test-provider-secret-" + key] = status
    code, body = request(router, ids[0])
    assert code == 200 and body["model"] == "deepseek/test"
    assert len(upstream.calls) == 5
    assert "Fallback active" in claude_settings_path().read_text()
    assert "deepseek/test" in claude_settings_path().read_text()
    assert "test-provider-secret-" not in state_path().read_text()
    restarted = FallbackState(persistent=True, clock=lambda: now[0])
    assert restarted.unavailable(ids[0])
    request(router, ids[0])
    assert len(upstream.calls) == 6  # Only the healthy tail is called during cooldown.
    upstream.faults.clear()
    now[0] += 11
    code, body = request(router, ids[0])
    assert code == 200 and body["model"] == "claude-opus-5"
    assert "CAM fallback active" not in body["content"][0]["text"]
    assert "Fallback active" not in claude_settings_path().read_text()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_rejections_do_not_spend_on_another_account(chain, status):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-max"] = status
    assert request(router, ids[0])[0] == status
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("fault", ["early-error", "empty-block-error"])
def test_early_sse_error_falls_back_and_preserves_tool_indexes(chain, fault):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-max"] = fault
    status, stream = request(router, ids[0], stream=True)
    assert status == 200
    assert sum(event["type"] == "message_start" for event in stream) == 1
    assert stream[0]["message"]["model"] == "claude-sonnet-5"
    starts = [event for event in stream if event["type"] == "content_block_start"]
    assert [(event["index"], event["content_block"]["type"]) for event in starts] == [
        (0, "text"),
        (1, "tool_use"),
    ]
    tool = next(
        event for event in stream if event.get("delta", {}).get("type") == "input_json_delta"
    )
    assert tool["index"] == 1
    assert json.loads(tool["delta"]["partial_json"]) == {"pattern": "*.txt"}
    assert stream[-1]["type"] == "message_stop"


def test_late_sse_error_never_replays_tool_output_and_next_request_falls_back(chain):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-max"] = "late-error"
    status, stream = request(router, ids[0], stream=True)
    assert status == 200 and stream[-1]["type"] == "error"
    assert len(upstream.calls) == 1
    assert sum(event.get("content_block", {}).get("type") == "tool_use" for event in stream) == 1
    assert request(router, ids[0])[1]["model"] == "claude-sonnet-5"


def test_concurrent_sessions_are_not_globally_switched(chain):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-max"] = 429
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda route: request(router, route), [ids[0], ids[2]] * 4))
    assert all(status == 200 for status, _ in results)
    assert [body["model"] for _, body in results] == ["claude-sonnet-5", "z-ai/glm-5.3-flash"] * 4


def test_timeout_falls_back(chain):
    router, upstream, ids, _now = chain
    router.fallback_timeout = 0.05
    upstream.faults["test-provider-secret-max"] = "timeout"
    assert request(router, ids[0])[1]["model"] == "claude-sonnet-5"


def test_fallback_preserves_images_and_tool_results_but_removes_foreign_thinking(chain):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-max"] = 429
    upstream.faults["test-provider-secret-personal"] = 429
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "synthetic-image",
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "foreign-signature"},
                {"type": "tool_use", "id": "previous-tool", "name": "Glob", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "previous-tool", "content": "file.txt"}
            ],
        },
    ]
    assert request(router, ids[0], messages=messages)[0] == 200
    forwarded = upstream.calls[-1][1]["messages"]
    assert forwarded[0] == messages[0]
    assert forwarded[1]["content"] == messages[1]["content"][1:]
    assert forwarded[2] == messages[2]


def test_upstream_stream_reset_is_not_mistaken_for_client_cancellation():
    from claude_auth_manager.fallback import UpstreamFailure

    class ResetStream:
        def readline(self, _limit):
            raise ConnectionResetError("synthetic reset")

    with pytest.raises(UpstreamFailure):
        list(events(ResetStream()))


def test_google_streaming_fallback_banner(chain):
    router, upstream, ids, _now = chain
    upstream.faults["test-provider-secret-router"] = 402
    _, stream = request(router, ids[2], stream=True)
    text = "".join(event.get("delta", {}).get("text", "") for event in stream)
    assert "CAM fallback active" in text and "GOOGLE_OK" in text


def test_cli_links_reload_without_restart_and_cycles_are_atomic(chain, capsys):
    router, upstream, ids, _now = chain
    assert cli.main(["select", "--fallback", ids[0], ids[2]]) == 0
    original = load_preferences()
    assert cli.main(["select", "--fallback", ids[2], ids[0]]) == 1
    assert load_preferences() == original
    upstream.faults["test-provider-secret-max"] = 429
    assert request(router, ids[0])[1]["model"] == "z-ai/glm-5.3-flash"
    assert cli.main(["select", "--clear-fallback", ids[0]]) == 0
    assert request(router, ids[0])[0] == 429
    assert "cycle" in capsys.readouterr().err


def test_every_route_down_has_bounded_attempts(chain):
    router, upstream, ids, _now = chain
    for credential in ("max", "personal", "router", "google", "backup"):
        upstream.faults["test-provider-secret-" + credential] = 503
    assert request(router, ids[0])[0] == 503
    assert len(upstream.calls) == 5
    assert request(router, ids[0])[0] == 503
    assert len(upstream.calls) == 5


def test_links_must_be_activated_and_acyclic():
    for links in ({"a": "a"}, {"a": "b", "b": "a"}, {"a": "c"}):
        with pytest.raises(ValueError):
            validate_links(links, {"a", "b"})


def test_retry_delay_honors_subscription_reset_and_retry_after():
    assert retry_delay("usage limit", {"Retry-After": "12"}, 1000) == 12
    assert (
        retry_delay(
            "usage limit",
            {
                "anthropic-ratelimit-unified-status": "rejected",
                "anthropic-ratelimit-unified-reset": "6000",
            },
            1000,
        )
        == 5000
    )
    assert retry_delay("provider unavailable", {"Retry-After": "nan"}, 1000) == 60
    assert retry_delay("credits exhausted", {}, 1000) == 900


def test_reselection_removes_dangling_links(isolated_home, managed_models):
    configure_claude(managed_models)
    a, b, c = map(managed_model, managed_models)
    save_fallbacks({a: b, b: c})
    configure_claude([managed_models[0], managed_models[2]])
    assert load_preferences()["fallbacks"] == {}
