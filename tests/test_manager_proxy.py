from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from claude_auth_manager.models import managed_model
from claude_auth_manager.proxy import LOCAL_TOKEN_HEADER, HybridRouterServer
from claude_auth_manager.registry import add_account_token, add_key

LOCAL_TOKEN = "local-router-token"


class RecordingProvider(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        record = {
            "path": self.path,
            "headers": {key.casefold(): value for key, value in self.headers.items()},
            "body": json.loads(body),
        }
        with self.lock:
            self.requests.append(record)
        if self.path.endswith("/chat/completions"):
            response = {
                "id": "chatcmpl_test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "google-ok"},
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            }
        else:
            response = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "provider-ok"}],
                "model": record["body"]["model"],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args: object) -> None:
        return


def _model(provider: str, credential: str, model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "provider": provider,
        "credential": credential,
    }


@contextmanager
def _running(server: ThreadingHTTPServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def manager_servers(isolated_home):
    add_key("openrouter", "router-a", "openrouter-secret-a")
    add_key("openrouter", "router-b", "openrouter-secret-b")
    add_key("google", "google-a", "google-secret-a")
    add_key("google", "google-b", "google-secret-b")
    add_key("anthropic-api", "api-a", "anthropic-secret-a")
    add_account_token("sub-a", "subscription-secret-a")
    models = [
        _model("openrouter", "router-a", "qwen/qwen3-coder"),
        _model("openrouter", "router-b", "qwen/qwen3-coder"),
        _model("google", "google-a", "gemini-test"),
        _model("google", "google-b", "gemini-test"),
        _model("anthropic-api", "api-a", "claude-sonnet-4-6"),
        _model("anthropic", "sub-a", "claude-sonnet-4-6"),
    ]
    routes = {managed_model(model): model for model in models}
    RecordingProvider.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingProvider)
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
    router = HybridRouterServer(
        ("127.0.0.1", 0),
        local_token=LOCAL_TOKEN,
        routes=routes,
        openrouter_upstream=f"{upstream_url}/openrouter/api",
        anthropic_upstream=f"{upstream_url}/anthropic",
        google_upstream=f"{upstream_url}/google/openai",
        record_status=False,
    )
    with _running(upstream), _running(router):
        yield router, models


def _request(
    router: HybridRouterServer,
    route: str,
    *,
    path: str = "/v1/messages",
    messages: list[dict[str, Any]] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", router.server_address[1], timeout=5)
    body = json.dumps(
        {
            "model": route,
            "messages": messages or [{"role": "user", "content": "hello"}],
            "max_tokens": 16,
        }
    )
    headers = {
        "Content-Type": "application/json",
        LOCAL_TOKEN_HEADER: LOCAL_TOKEN,
        "Authorization": "Bearer attacker-controlled",
        "X-Api-Key": "attacker-controlled",
    }
    headers.update(extra_headers or {})
    connection.request(
        "POST",
        path,
        body=body,
        headers=headers,
    )
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def test_same_live_session_hot_switches_models_per_request(manager_servers) -> None:
    router, models = manager_servers
    session_headers = {"X-Claude-Code-Session-Id": "same-live-session"}
    selected = [models[0], models[2], models[5], models[0]]

    for model in selected:
        assert _request(
            router,
            managed_model(model),
            extra_headers=session_headers,
        )[0] == 200

    requests = RecordingProvider.requests
    assert [request["body"]["model"] for request in requests] == [
        "qwen/qwen3-coder",
        "gemini-test",
        "claude-sonnet-4-6",
        "qwen/qwen3-coder",
    ]
    assert all(
        request["headers"]["x-claude-code-session-id"] == "same-live-session"
        for request in requests
    )
    openrouter_notices = [
        request["body"]["system"]
        for request in (requests[0], requests[3])
    ]
    assert all(
        "qwen/qwen3-coder via claude-auth-manager (cam)" in notice
        for notice in openrouter_notices
    )
    assert "gemini-test via claude-auth-manager (cam)" in requests[1]["body"]["messages"][0][
        "content"
    ]
    assert "claude-sonnet-4-6 via claude-auth-manager (cam)" in requests[2]["body"]["system"]


def test_each_route_uses_only_its_exact_credential(manager_servers) -> None:
    router, models = manager_servers
    for model in models:
        status, _payload = _request(router, managed_model(model))
        assert status == 200

    by_model = {
        (record["path"], record["body"]["model"], record["headers"].get("authorization")): record
        for record in RecordingProvider.requests
    }
    router_a = by_model[
        ("/openrouter/api/v1/messages", "qwen/qwen3-coder", "Bearer openrouter-secret-a")
    ]
    router_b = by_model[
        ("/openrouter/api/v1/messages", "qwen/qwen3-coder", "Bearer openrouter-secret-b")
    ]
    google = by_model[("/google/openai/chat/completions", "gemini-test", "Bearer google-secret-a")]
    google_b = by_model[
        ("/google/openai/chat/completions", "gemini-test", "Bearer google-secret-b")
    ]
    subscription = by_model[
        ("/anthropic/v1/messages", "claude-sonnet-4-6", "Bearer subscription-secret-a")
    ]
    anthropic_api = next(
        record
        for record in RecordingProvider.requests
        if record["headers"].get("x-api-key") == "anthropic-secret-a"
    )

    assert router_a["headers"].get("x-api-key") is None
    assert router_b["headers"].get("x-api-key") is None
    assert router_a["headers"].get("http-referer") is None
    assert router_b["headers"].get("http-referer") is None
    assert google["headers"].get("x-api-key") is None
    assert google_b["headers"].get("x-api-key") is None
    assert subscription["headers"].get("x-api-key") is None
    assert anthropic_api["headers"].get("authorization") is None
    assert all(
        LOCAL_TOKEN_HEADER.casefold() not in record["headers"]
        for record in RecordingProvider.requests
    )


def test_same_model_can_run_concurrently_through_two_openrouter_keys(manager_servers) -> None:
    router, models = manager_servers
    openrouter = [model for model in models if model["provider"] == "openrouter"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda model: _request(router, managed_model(model)),
                openrouter * 4,
            )
        )

    assert all(status == 200 for status, _ in results)
    authorizations = [
        record["headers"].get("authorization")
        for record in RecordingProvider.requests
        if record["path"].startswith("/openrouter/")
    ]
    assert authorizations.count("Bearer openrouter-secret-a") == 4
    assert authorizations.count("Bearer openrouter-secret-b") == 4


def test_reselection_keeps_retained_agent_route_and_rejects_removed_route(
    manager_servers,
) -> None:
    router, models = manager_servers
    openrouter = [model for model in models if model["provider"] == "openrouter"]
    route_a, route_b = [managed_model(model) for model in openrouter]

    assert _request(router, route_a)[0] == 200
    assert _request(router, route_b)[0] == 200

    # This is the route allowlist seen by subsequent agent requests after
    # `cam select` reconfigures/restarts the local router.
    router.routes = {route_b: openrouter[1]}

    removed_status, removed_payload = _request(router, route_a)
    retained_status, retained_payload = _request(router, route_b)
    assert removed_status == 400
    assert "trusted route" in removed_payload["error"]["message"]
    assert retained_status == 200
    assert retained_payload["content"][0]["text"] == "provider-ok"


def test_google_response_is_translated_back_to_anthropic(manager_servers) -> None:
    router, models = manager_servers
    google = next(model for model in models if model["provider"] == "google")
    status, payload = _request(router, managed_model(google))

    assert status == 200
    assert payload["type"] == "message"
    assert payload["model"] == "gemini-test"
    assert payload["content"] == [{"type": "text", "text": "google-ok"}]
    assert payload["usage"] == {"input_tokens": 4, "output_tokens": 2}


def test_google_thought_metadata_is_isolated_per_key(manager_servers) -> None:
    router, models = manager_servers
    google = [model for model in models if model["provider"] == "google"]
    router.google_metadata("google-a")["call_shared"] = {
        "google": {"thought_signature": "signature-a"}
    }
    router.google_metadata("google-b")["call_shared"] = {
        "google": {"thought_signature": "signature-b"}
    }
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_shared",
                    "name": "lookup",
                    "input": {"query": "weather"},
                }
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_shared", "content": "sunny"}],
        },
    ]

    for model in google:
        status, _payload = _request(router, managed_model(model), messages=messages)
        assert status == 200

    requests = [
        record
        for record in RecordingProvider.requests
        if record["path"].endswith("/chat/completions")
    ]
    signatures = {}
    for record in requests:
        assistant = next(
            message
            for message in record["body"]["messages"]
            if message.get("role") == "assistant"
        )
        signatures[record["headers"]["authorization"]] = assistant["tool_calls"][0][
            "extra_content"
        ]["google"]["thought_signature"]
    assert signatures["Bearer google-secret-a"] == "signature-a"
    assert signatures["Bearer google-secret-b"] == "signature-b"


def test_google_count_tokens_is_local_and_does_not_send_key(manager_servers) -> None:
    router, models = manager_servers
    google = next(model for model in models if model["provider"] == "google")
    before = len(RecordingProvider.requests)

    status, payload = _request(
        router,
        managed_model(google),
        path="/v1/messages/count_tokens",
    )

    assert status == 200
    assert payload["input_tokens"] > 0
    assert len(RecordingProvider.requests) == before


def test_unselected_or_malformed_routes_fail_closed_without_upstream_call(
    manager_servers,
) -> None:
    router, _models = manager_servers
    before = len(RecordingProvider.requests)

    status, payload = _request(router, "cam/openrouter/router-a/not-selected/model")

    assert status == 400
    assert "trusted route" in payload["error"]["message"]
    assert len(RecordingProvider.requests) == before
