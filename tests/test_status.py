from __future__ import annotations

import io
import json
import subprocess
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_auth_manager import cli, status
from claude_auth_manager.registry import add_account_token, add_key


@pytest.fixture
def credentials(isolated_home):
    add_account_token("primary", "synthetic-subscription-token")
    add_account_token("backup", "synthetic-backup-token")
    add_key("openrouter", "router", "synthetic-openrouter-key")
    add_key("google", "google", "synthetic-google-key")
    add_key("anthropic-api", "api", "synthetic-anthropic-key")


def response(url, headers):
    if url.endswith("/api/oauth/usage"):
        return {"five_hour": {"utilization": 25, "resets_at": "2030-01-01T00:00:00Z"}}
    if url.endswith("/key"):
        return {"data": {"limit": 10, "limit_remaining": 7.25, "usage": 2.75}}
    return {"data": [{"id": "model"}]}


def test_all_includes_unselected_credentials_and_uses_provider_auth(credentials, monkeypatch):
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        return response(url, headers)

    monkeypatch.setattr(status, "_get", get)
    result = status.check_credentials()
    assert result["passed"] and not result["billable"]
    assert len(result["credentials"]) == 5 and len(calls) == 5
    assert all("synthetic-" not in json.dumps(row) for row in result["credentials"])
    row = next(r for r in result["credentials"] if r["id"] == "router")
    assert row["usage"]["limit_remaining"] == 7.25
    assert row["quota_available"] is None  # Key budget is not the account's balance.
    assert any(h.get("x-api-key") == "synthetic-anthropic-key" for _, h in calls)
    assert any(h.get("Authorization") == "Bearer synthetic-google-key" for _, h in calls)


@pytest.mark.parametrize(
    ("kwargs", "count"),
    [({"account": ""}, 2), ({"key": ""}, 3), ({"account": "primary"}, 1), ({"key": "router"}, 1)],
)
def test_filters(credentials, monkeypatch, kwargs, count):
    monkeypatch.setattr(status, "_get", response)
    assert len(status.check_credentials(**kwargs)["credentials"]) == count


def test_invalid_filter_never_sends_requests(credentials, monkeypatch):
    monkeypatch.setattr(status, "_get", lambda *_: pytest.fail("unexpected request"))
    with pytest.raises(ValueError, match="no saved credential"):
        status.check_credentials(key="missing")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "unauthorized"),
        (402, "limited"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (503, "unavailable"),
    ],
)
def test_failures_are_per_credential_and_do_not_echo_responses(
    credentials, monkeypatch, code, expected
):
    def get(url, headers):
        if url.endswith("/key"):
            raise urllib.error.HTTPError(
                url, code, "secret-message", {}, io.BytesIO(b"secret-body")
            )
        return response(url, headers)

    monkeypatch.setattr(status, "_get", get)
    result = status.check_credentials()
    assert not result["passed"] and len(result["credentials"]) == 5
    assert next(r for r in result["credentials"] if r["id"] == "router")["status"] == expected
    assert "secret" not in json.dumps(result)


def test_refresh_timeout_does_not_abort_other_accounts(credentials, monkeypatch):
    original = status.read_account_token

    def token(name):
        if name == "primary":
            raise subprocess.TimeoutExpired("private-command", 30)
        return original(name)

    monkeypatch.setattr(status, "read_account_token", token)
    monkeypatch.setattr(status, "_get", response)
    result = status.check_credentials()
    assert len(result["credentials"]) == 5
    assert next(r for r in result["credentials"] if r["id"] == "primary")["status"] == "local_error"
    assert "private-command" not in json.dumps(result)


def test_reached_limits_and_unknown_quota(credentials, monkeypatch):
    def get(url, headers):
        if url.endswith("/key"):
            return {"data": {"limit_remaining": 0}}
        if url.endswith("/api/oauth/usage"):
            return {"seven_day_sonnet": {"utilization": 100}}
        return {"data": []}

    monkeypatch.setattr(status, "_get", get)
    rows = status.check_credentials()["credentials"]
    assert sum(r["status"] == "limited" for r in rows) == 3
    assert all(r["quota_available"] is None for r in rows if r["status"] == "valid")


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": "secret"}])
def test_invalid_metadata_is_not_reported_valid(credentials, monkeypatch, payload):
    monkeypatch.setattr(status, "_get", lambda *_: payload)
    row = status.check_credentials(key="google")["credentials"][0]
    assert row["status"] == "invalid_response" and "secret" not in json.dumps(row)


def test_cli_json_and_table_are_noninteractive(credentials, monkeypatch, capsys):
    monkeypatch.setattr(status, "_get", response)
    assert cli.main(["check", "--all", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["credentials"]) == 5
    assert cli.main(["check", "--account"]) == 0
    table = capsys.readouterr().out
    assert "25% used" in table and "google" not in table
    assert cli.main(["check", "some/model", "--all"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_http_get_only_and_redirects_never_forward_credentials():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            calls.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            status._get(
                f"http://127.0.0.1:{server.server_port}/status", {"Authorization": "secret"}
            )
        assert calls == ["/status"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
