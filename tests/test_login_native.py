"""Opt-in smoke check using the real Claude binary in an offline container.

Set CAM_TEST_CLAUDE_BINARY only in a --network none container. No real account is
used: the callback has a fake code and an HTTP proxy rejects the token exchange.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAM_TEST_CLAUDE_BINARY"), reason="requires offline native-CLI container"
)


@pytest.mark.parametrize("ssh", [False, True])
@pytest.mark.parametrize("hosted_url", [False, True])
def test_native_claude_opens_only_hosted_url_and_reaches_token_exchange(
    isolated_home, tmp_path, monkeypatch, ssh, hosted_url
) -> None:
    attempted = []

    class RejectProxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            attempted.append(self.path)
            self.send_error(502, "Offline test: intentionally no token exchange")

        def log_message(self, *args):
            pass

    browser_url = tmp_path / "browser-url"
    monkeypatch.setenv("CAM_TEST_BROWSER_URL", str(browser_url))
    monkeypatch.setenv("BROWSER", str(Path(__file__).parent / "helpers" / "capture_browser.py"))
    if ssh:
        monkeypatch.setenv("SSH_CONNECTION", "192.0.2.1 22222 192.0.2.2 22")
    else:
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    for name in (
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        monkeypatch.setenv(name, "1")
    with ThreadingHTTPServer(("127.0.0.1", 0), RejectProxy) as proxy:
        worker = threading.Thread(target=proxy.serve_forever, daemon=True)
        worker.start()
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
        driver = (
            "import os,sys; from claude_auth_manager.login import run_login; "
            "from claude_auth_manager.registry import _clean_auth_environment; "
            "sys.exit(run_login([os.environ['CAM_TEST_CLAUDE_BINARY'], "
            "'auth','login','--claudeai'], env=_clean_auth_environment()))"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", driver],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        try:
            output = b""
            deadline = time.monotonic() + 20
            while b"full callback URL (hidden):" not in output or not browser_url.exists():
                assert time.monotonic() < deadline, "Native login did not show the CAM prompt"
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if ready:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    assert chunk, "Native CLI exited before its login prompt"
                    output += chunk
            launched_urls = browser_url.read_text().splitlines()
            assert len(launched_urls) == 1
            launched = launched_urls[0]
            query = parse_qs(urlsplit(launched).query)
            redirect = query["redirect_uri"][0]
            state = query["state"][0]
            assert redirect == "https://platform.claude.com/oauth/code/callback"
            assert launched.encode() in output  # Exactly the printed manual URL, not a rewrite.
            before = attempted.count("platform.claude.com:443")
            code = "cam-offline-test-not-a-real-authorization-code"
            value = (
                redirect + "?" + urlencode({"code": code, "state": state})
                if hosted_url
                else f"{code}#{state}"
            )
            after, _ = process.communicate((value + "\n").encode(), timeout=40)
            output += after
            assert process.returncode != 0  # Deliberate rejection, never a fake successful login.
            assert b"Login failed:" in output
            assert b"Invalid code. Please make sure" not in output
            assert b"different login" not in output
            assert b"Could not reach" not in output
            assert code.encode() not in output
            assert attempted.count("platform.claude.com:443") > before
            assert browser_url.read_text().splitlines() == [launched]
            assert not (isolated_home / ".claude" / ".credentials.json").exists()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            proxy.shutdown()
            worker.join()
