from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from claude_auth_manager import login

STATE = "test-login-state"
CODE = "test-private-code"
HELPER = Path(__file__).parent / "helpers" / "fake_claude_login.py"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_callback_normalizes_only_loopback(host: str) -> None:
    assert login.parse_login_response(
        f"http://{host}:54321/callback?code=a%2Bb%3Dc&state={STATE}", STATE
    ) == (54321, f"/callback?code=a%2Bb%3Dc&state={STATE}")


def test_manual_code_is_passed_intact() -> None:
    assert login.parse_login_response(f"  {CODE}#{STATE}\n", STATE) == (None, f"{CODE}#{STATE}")


def test_hosted_callback_url_becomes_manual_code_without_a_network_request() -> None:
    url = login._HOSTED_CALLBACK + "?" + urlencode({"code": CODE, "state": STATE})
    assert login.parse_login_response(url, STATE) == (None, f"{CODE}#{STATE}")


@pytest.mark.parametrize(
    "url",
    [
        "https://platform.claude.com.evil/oauth/code/callback",
        "https://platform.claude.com/other",
        "https://platform.claude.com:1234/oauth/code/callback",
        "https://user@platform.claude.com/oauth/code/callback",
        "https://other.example.com/oauth/code/callback",
        "http://platform.claude.com/oauth/code/callback",
    ],
)
def test_hosted_callback_requires_exact_current_redirect(url) -> None:
    with pytest.raises(ValueError):
        login.parse_login_response(url + "?" + urlencode({"code": CODE, "state": STATE}), STATE)


def test_hosted_callback_rejects_wrong_state_or_ambiguous_code() -> None:
    for query in ({"code": CODE, "state": "stale"}, {"code": "a#b", "state": STATE}):
        with pytest.raises(ValueError):
            login.parse_login_response(login._HOSTED_CALLBACK + "?" + urlencode(query), STATE)


def test_hosted_authorization_never_opens_automatic_loopback_url() -> None:
    for redirect in [login._HOSTED_CALLBACK, "http://localhost:54321/callback"]:
        url = "https://claude.com/cai/oauth/authorize?" + urlencode(
            {
                "code": "true",
                "response_type": "code",
                "code_challenge": "test",
                "code_challenge_method": "S256",
                "state": STATE,
                "redirect_uri": redirect,
            }
        )
        assert login._hosted_authorization(url) == (redirect == login._HOSTED_CALLBACK)


def test_browser_keeps_original_environment_and_opens_once(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "browser with spaces"
    executable.touch()
    env = {"BROWSER": str(executable), "DISPLAY": ":test"}
    calls = []

    class Child:
        def poll(self):
            return 0

    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return Child()

    monkeypatch.setattr(login.subprocess, "Popen", spawn)
    browser = login._HostedBrowser(env)
    browser.open("https://claude.com/test")
    browser.open("https://claude.com/duplicate")
    assert len(calls) == 1
    assert calls[0][0] == [str(executable), "https://claude.com/test"]
    assert calls[0][1]["env"] == env
    assert calls[0][1].get("shell", False) is False
    browser.poll()
    assert browser.process is None


def test_browser_command_template_does_not_use_shell(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(login.subprocess, "Popen", lambda command, **kw: calls.append(command))
    browser = login._HostedBrowser({"BROWSER": "browser --new-window '%s'"})
    browser.open("https://claude.com/test?a=1&b=2")
    assert calls == [["browser", "--new-window", "https://claude.com/test?a=1&b=2"]]


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com:54321/callback?code=test-private-code&state=test-login-state",
        "https://localhost:54321/callback?code=test-private-code&state=test-login-state",
        "http://localhost.evil:54321/callback?code=test-private-code&state=test-login-state",
        "http://localhost:54321/other?code=test-private-code&state=test-login-state",
        "http://user@localhost:54321/callback?code=test-private-code&state=test-login-state",
        "http://localhost:54321/callback?code=test-private-code&state=test-login-state#extra",
        "http://localhost/callback?code=test-private-code&state=test-login-state",
        "http://localhost:0/callback?code=test-private-code&state=test-login-state",
        "http://localhost:99999/callback?code=test-private-code&state=test-login-state",
        "http://localhost:54321/callback?code=test-private-code&state=stale",
        "http://localhost:54321/callback?code=test-private-code&state=test-login-state&state=extra",
        "http://localhost:54321/callback?code=test-private-code&code=extra&state=test-login-state",
        "http://localhost:54321/callback?code=test-private-code",
        "http://localhost:54321/callback?code=&state=test-login-state",
        "http://localhost:54321/callback?code=%00&state=test-login-state",
        "http://localhost:54321/callback?code=%0A&state=test-login-state",
        "test-private-code#stale",
        "test-private-code",
        "test-private-code#test-login-state#extra",
        "test-private-code#test-login-state\nextra",
        "test-private-code#test-login-state\x1b",
        "x" * 8193,
    ],
)
def test_invalid_responses_are_rejected_without_disclosing_secret(value: str) -> None:
    with pytest.raises(ValueError) as error:
        login.parse_login_response(value, STATE)
    assert CODE not in str(error.value)
    assert value not in str(error.value)


def test_no_response_before_authorize_state() -> None:
    with pytest.raises(ValueError, match="not ready"):
        login.parse_login_response(f"{CODE}#{STATE}", None)


@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
def test_output_tracks_state_and_replaces_split_prompt(chunk_size, capsys) -> None:
    output = login._Output()
    url = f"https://claude.com/oauth/authorize?response_type=code&code_challenge=x&state={STATE}"
    text = f"Login: \x1b]8;;{url}\x07{url}\x1b]8;;\x07\n{login._CLAUDE_PROMPT}"
    for offset in range(0, len(text), chunk_size):
        output.feed(text[offset : offset + chunk_size])
    assert output.ready
    assert output.expected_state == STATE
    captured = capsys.readouterr().out
    assert login._PROMPT in captured
    assert login._CLAUDE_PROMPT not in captured
    assert "hosted page" in captured


def test_mask_matches_length_and_handles_backspace_and_bracketed_paste(capsys) -> None:
    entry = login._Input(True)
    text = b"x\x7f\x1b[200~test-private-code#test-login-state\x1b[201~\n"
    result = None
    for byte in text:
        result = entry.feed(bytes([byte]))
    assert result == f"{CODE}#{STATE}"
    captured = capsys.readouterr().out
    assert captured == "*\b \b" + "*" * len(result)
    assert CODE not in captured


def test_input_size_is_bounded_and_can_retry() -> None:
    entry = login._Input(False)
    for _ in range(9000):
        entry.feed(b"x")
    assert len(entry.value) == 8192
    with pytest.raises(ValueError, match="too long"):
        entry.feed(b"\n")
    entry.feed(b"a")
    assert entry.feed(b"\n") == "a"


def test_terminal_does_not_return_unread_secret_to_shell() -> None:
    master, slave = os.openpty()
    original = termios.tcgetattr(slave)
    try:
        with login._masked_terminal(slave):
            os.write(master, b"unread-private-callback\n")
            assert select.select([slave], [], [], 1)[0]
        assert termios.tcgetattr(slave) == original
        assert not select.select([slave], [], [], 0)[0]
    finally:
        os.close(master)
        os.close(slave)


def _read_until(fd: int, marker: bytes, timeout: float = 8) -> bytes:
    deadline = time.monotonic() + timeout
    result = b""
    while marker not in result:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(f"Login did not reach {marker!r}; output: {result!r}")
        readable, _, _ = select.select([fd], [], [], remaining)
        if readable:
            data = os.read(fd, 4096)
            if not data:
                pytest.fail(f"Login exited before {marker!r}; output: {result!r}")
            result += data
    return result


@contextlib.contextmanager
def _session(tmp_path, monkeypatch, mode="callback", *, terminal=False):
    root = tmp_path / "login"
    root.mkdir()
    monkeypatch.setenv("CAM_TEST_LOGIN_DIR", str(root))
    monkeypatch.setenv("CAM_TEST_LOGIN_MODE", mode)
    monkeypatch.setenv("CAM_TEST_BROWSER_URL", str(root / "browser-url"))
    monkeypatch.setenv("BROWSER", str(HELPER.with_name("capture_browser.py")))
    # Deliberately invalid proxy settings must not affect a local callback.
    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://192.0.2.1:1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    driver = (
        "import os,sys; from claude_auth_manager.login import run_login; "
        "sys.exit(run_login([sys.executable, sys.argv[1]], env=dict(os.environ)))"
    )
    master, slave = os.openpty() if terminal else (None, None)
    original = termios.tcgetattr(slave) if terminal else None
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", driver, str(HELPER)],
        stdin=slave if terminal else subprocess.PIPE,
        stdout=slave if terminal else subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    try:
        yield process, root, master, slave, original
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        if terminal:
            os.close(master)
            os.close(slave)


@pytest.mark.parametrize("mode", ["callback", "exchange-error"])
def test_failed_browser_url_completes_through_native_listener(
    isolated_home, tmp_path, monkeypatch, mode
) -> None:
    with _session(tmp_path, monkeypatch, mode) as (process, root, *_):
        before = _read_until(process.stdout.fileno(), login._PROMPT.encode())
        port = int(root.joinpath("port").read_text())
        callback = f"http://localhost:{port}/callback?code={CODE}&state={STATE}"
        after, _ = process.communicate((callback + "\n").encode(), timeout=8)
        assert process.returncode == (1 if mode == "exchange-error" else 0)
        assert root.joinpath("callback-received").exists()
        assert CODE.encode() not in before + after
        profile = isolated_home / ".claude" / ".credentials.json"
        assert profile.exists() == (mode == "callback")


def test_wrong_session_and_bad_url_can_retry_without_touching_listener(
    isolated_home, tmp_path, monkeypatch
) -> None:
    with _session(tmp_path, monkeypatch) as (process, root, *_):
        _read_until(process.stdout.fileno(), login._PROMPT.encode())
        port = int(root.joinpath("port").read_text())
        callback = f"http://localhost:{port}/callback?code={CODE}&state=wrong"
        process.stdin.write((callback + "\n").encode())
        output = _read_until(process.stdout.fileno(), login._PROMPT.encode())
        assert b"different login" in output
        assert not root.joinpath("callback-received").exists()
        process.stdin.write(b"https://example.com/\n")
        _read_until(process.stdout.fileno(), login._PROMPT.encode())
        assert not root.joinpath("callback-received").exists()
        after, _ = process.communicate(
            (callback.replace("state=wrong", f"state={STATE}") + "\n").encode(), timeout=8
        )
        assert process.returncode == 0
        assert CODE.encode() not in output + after


@pytest.mark.parametrize("hosted_url", [False, True])
def test_manual_code_pipe(isolated_home, tmp_path, monkeypatch, hosted_url) -> None:
    with _session(tmp_path, monkeypatch, "manual") as (process, root, *_):
        value = (
            login._HOSTED_CALLBACK + "?" + urlencode({"code": CODE, "state": STATE})
            if hosted_url
            else f"{CODE}#{STATE}"
        )
        output, _ = process.communicate((value + "\n").encode(), timeout=8)
        assert process.returncode == 0
        assert b"Login successful" in output
        assert CODE.encode() not in output
        urls = root.joinpath("browser-url").read_text().splitlines()
        assert len(urls) == 1
        assert parse_qs(urlsplit(urls[0]).query)["redirect_uri"] == [
            "https://platform.claude.com/oauth/code/callback"
        ]


@pytest.mark.parametrize("mode", ["manual", "callback", "automatic"])
def test_real_terminal_mask_and_automatic_completion(
    isolated_home, tmp_path, monkeypatch, mode
) -> None:
    with _session(tmp_path, monkeypatch, mode, terminal=True) as (
        process,
        root,
        master,
        slave,
        original,
    ):
        output = _read_until(master, login._PROMPT.encode())
        if mode == "automatic":
            value = "partially-typed-secret"
        elif mode == "manual":
            value = f"{CODE}#{STATE}"
        else:
            port = int(root.joinpath("port").read_text())
            value = f"http://localhost:{port}/callback?code={CODE}&state={STATE}"
        os.write(master, (value + ("\n" if mode != "automatic" else "")).encode())
        output += _read_until(master, b"Login successful")
        process.wait(timeout=5)
        assert process.returncode == 0
        assert termios.tcgetattr(slave) == original
        assert b"*" * len(value) in output
        assert value.encode() not in output
        assert CODE.encode() not in output


@pytest.mark.parametrize("cancel", ["eof", "sigint"])
def test_cancel_restores_terminal_and_reaps_login_child(
    isolated_home, tmp_path, monkeypatch, cancel
) -> None:
    with _session(tmp_path, monkeypatch, terminal=True) as (process, root, master, slave, original):
        _read_until(master, login._PROMPT.encode())
        pid = int(root.joinpath("pid").read_text())
        if cancel == "eof":
            os.write(master, b"\x04")
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=5)
        assert process.returncode != 0
        assert termios.tcgetattr(slave) == original
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_native_error_does_not_wait_for_input(isolated_home, tmp_path, monkeypatch) -> None:
    with _session(tmp_path, monkeypatch, "early-error") as (process, *_):
        process.wait(timeout=5)
        output = process.stdout.read()
        assert process.returncode == 7
        assert b"test failure" in output
