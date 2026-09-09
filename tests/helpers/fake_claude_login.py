#!/usr/bin/env python3
"""Offline OAuth CLI stand-in: no network beyond loopback and no real credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

if "status" in sys.argv:
    print(
        json.dumps(
            {
                "loggedIn": True,
                "apiProvider": "firstParty",
                "authMethod": "claude.ai",
                "email": "person@example.com",
                "subscriptionType": "max",
            }
        )
    )
    sys.exit(0)

mode = os.environ.get("CAM_TEST_LOGIN_MODE", "callback")
root = Path(os.environ["CAM_TEST_LOGIN_DIR"])
root.joinpath("pid").write_text(str(os.getpid()))
state = "test-login-state"
code = "test-private-code"


def save() -> None:
    profile = Path(os.environ["CLAUDE_CONFIG_DIR"])
    profile.mkdir(parents=True, exist_ok=True)
    profile.joinpath(".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": os.environ.get("CAM_TEST_ACCESS_TOKEN", "fake-access"),
                    "refreshToken": "fake-refresh",
                }
            }
        )
    )
    metadata = profile / ".claude.json"
    metadata.write_text(json.dumps({"oauthAccount": {"emailAddress": "person@example.com"}}))
    metadata.chmod(0o600)


if mode == "early-error":
    print("Login failed: test failure", file=sys.stderr, flush=True)
    sys.exit(7)

done = threading.Event()


class Callback(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        assert url.path == "/callback"
        assert parse_qs(url.query) == {"code": [code], "state": [state]}
        root.joinpath("callback-received").touch()
        if mode != "exchange-error":
            save()
        self.send_response(302)
        # A client following redirects would hit this listener again and fail.
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/forbidden")
        self.end_headers()
        done.set()

    def log_message(self, *args: object) -> None:
        pass


with HTTPServer(("127.0.0.1", 0), Callback) as server:
    root.joinpath("port").write_text(str(server.server_port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Opening browser to sign in…", flush=True)
    print(
        "If the browser didn't open, visit: https://claude.com/oauth/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "code_challenge": "test-challenge",
                "code_challenge_method": "S256",
                "state": state,
                "redirect_uri": "https://platform.claude.com/oauth/code/callback",
            }
        ),
        flush=True,
    )
    print("Paste code here if prompted > ", end="", flush=True)
    # Reproduce the root bug: native auth opens a different, loopback URL.
    subprocess.run(
        [
            os.environ["BROWSER"],
            "https://claude.com/oauth/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "code_challenge": "test-challenge",
                    "code_challenge_method": "S256",
                    "state": state,
                    "redirect_uri": f"http://localhost:{server.server_port}/callback",
                }
            ),
        ],
        check=True,
    )
    if mode == "manual":
        assert sys.stdin.readline().strip() == f"{code}#{state}"
        save()
    elif mode == "automatic":
        time.sleep(0.3)
        save()
    else:
        if not done.wait(timeout=15):
            sys.exit(8)
    server.shutdown()
    thread.join()
    if mode == "exchange-error":
        print("Login failed: authorization expired", flush=True)
        sys.exit(1)
    print("Login successful.", flush=True)
