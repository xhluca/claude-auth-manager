"""Billable, isolated real-account probes behind a loopback fault gateway.

Uses one selected model per registered credential. Simulates exhaustion on all
earlier routes, then lets real Claude Code complete a Glob round-trip on the tail.
No production favorites, fallback links, or running services are modified.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from claude_auth_manager import check
from claude_auth_manager.models import managed_model
from claude_auth_manager.proxy import HOP_BY_HOP, HybridRouterServer
from claude_auth_manager.registry import read_account_token, read_key
from claude_auth_manager.settings import favorite_models


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        token = self.headers.get("Authorization", "").removeprefix("Bearer ") or self.headers.get(
            "X-Api-Key", ""
        )
        allowed = fingerprint(token) == self.server.allowed
        self.server.attempts.append(allowed)
        if not allowed:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "300")
            self.end_headers()
            self.wfile.write(
                b'{"type":"error","error":{"type":"rate_limit_error",'
                b'"message":"Synthetic subscription usage limit"}}'
            )
            return
        prefix, _, tail = self.path.lstrip("/").partition("/")
        host, base = {
            "anthropic": ("api.anthropic.com", ""),
            "openrouter": ("openrouter.ai", "/api"),
            "google": ("generativelanguage.googleapis.com", "/v1beta/openai"),
        }[prefix]
        connection = http.client.HTTPSConnection(host, timeout=180)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.casefold() not in HOP_BY_HOP | {"host", "content-length"}
        }
        try:
            connection.request("POST", base + "/" + tail, body, headers)
            response = connection.getresponse()
            self.server.real_statuses.append(response.status)
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.casefold() not in HOP_BY_HOP | {"content-length", "server", "date"}:
                    self.send_header(key, value)
            self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            connection.close()


def main():
    selected = {}
    for model in favorite_models():
        selected.setdefault((model["provider"], model["credential"]), model)
    models = list(selected.values())
    if len(models) < 2:
        raise SystemExit("Need at least two selected credentials for a real fallback test.")
    successes = 0
    for index, sink in enumerate(models):
        ordered = [model for model in models if model is not sink] + [sink]
        routes = {managed_model(model): model for model in ordered}
        ids = list(routes)
        links = dict(zip(ids[:-1], ids[1:], strict=True))
        try:
            token = (
                read_account_token(sink["credential"])
                if sink["provider"] == "anthropic"
                else read_key(sink["credential"], provider=sink["provider"])
            )
        except (RuntimeError, OSError):
            print(
                json.dumps(
                    {
                        "destination": index + 1,
                        "provider": sink["provider"],
                        "passed": False,
                        "blocked": "saved credential could not be refreshed",
                    }
                ),
                flush=True,
            )
            continue
        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        gateway.allowed = fingerprint(token)
        gateway.attempts = []
        gateway.real_statuses = []
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{gateway.server_port}"

        def router_factory(
            address, *, local_token, bound_routes=routes, links=links, url=url, **_kwargs
        ):
            return HybridRouterServer(
                address,
                local_token=local_token,
                routes=bound_routes,
                fallbacks=links,
                anthropic_upstream=url + "/anthropic",
                openrouter_upstream=url + "/openrouter",
                google_upstream=url + "/google",
                record_status=False,
            )

        original = check.HybridRouterServer
        check.HybridRouterServer = router_factory
        try:
            # Unavailable simulated predecessors don't need usable real credentials.
            # Only the healthy tail receives an actual token, in this test process.
            def account_token(credential, token=token, sink=sink):
                return (
                    token
                    if sink["provider"] == "anthropic" and credential == sink["credential"]
                    else "synthetic-exhausted-account-token"
                )

            def provider_key(credential, *, provider, token=token, sink=sink):
                return (
                    token
                    if provider == sink["provider"] and credential == sink["credential"]
                    else "synthetic-exhausted-provider-token"
                )

            announcements = []
            original_parser = check.parse_probe_result

            def parse_probe(
                output,
                *args,
                original_parser=original_parser,
                announcements=announcements,
                **kwargs,
            ):
                announcements.append("CAM fallback active" in output)
                return original_parser(output, *args, **kwargs)

            with (
                patch("claude_auth_manager.proxy.read_account_token", account_token),
                patch("claude_auth_manager.proxy.read_key", provider_key),
                patch("claude_auth_manager.check.parse_probe_result", parse_probe),
            ):
                result = check.probe_model(ordered[0], timeout=120)
            announced = any(announcements)
            passed = (
                result.passed
                and announced
                and False in gateway.attempts
                and True in gateway.attempts
            )
            successes += int(passed)
            print(
                json.dumps(
                    {
                        "destination": index + 1,
                        "provider": sink["provider"],
                        "passed": passed,
                        "tool_called": result.tool_called,
                        "tool_completed": result.tool_completed,
                        "acknowledged": result.acknowledged_result,
                        "fallback_banner": announced,
                        "injected_failures": gateway.attempts.count(False),
                        "live_calls": gateway.attempts.count(True),
                        "upstream_statuses": gateway.real_statuses,
                    }
                ),
                flush=True,
            )
        finally:
            check.HybridRouterServer = original
            gateway.shutdown()
            gateway.server_close()
            thread.join(2)
    raise SystemExit(0 if successes == len(models) else 1)


if __name__ == "__main__":
    main()
