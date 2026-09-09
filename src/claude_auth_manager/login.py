"""Terminal adapter for Claude's OAuth login, including cross-host callbacks.

Claude owns PKCE, token exchange, credential storage, and refresh. CAM opens the
hosted-code authorization URL that Claude prints, not its automatic loopback
URL. Manual codes go to stdin; legacy callback URLs go to the loopback listener,
preserving the redirect URI used to issue that authorization code.
"""

from __future__ import annotations

import codecs
import contextlib
import http.client
import os
import re
import secrets
import select
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

_CLAUDE_PROMPT = "Paste code here if prompted > "
_PROMPT = "Paste login code or full callback URL (hidden): "
_ANSI = re.compile(r"\x1b\][^\x07]*?(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_INPUT = 8192
_HOSTED_CALLBACK = "https://platform.claude.com/oauth/code/callback"


class _HostedBrowser:
    """Open exactly Claude's hosted-code link, once, using the user's opener."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env = dict(env)
        self.opened = False
        self.process: subprocess.Popen[bytes] | None = None

    def open(self, url: str) -> None:
        if self.opened:
            return
        self.opened = True
        browser = self.env.get("BROWSER")
        try:
            if browser:
                # Preserve executable paths containing spaces as well as the
                # conventional BROWSER command/argument form. Never use a shell.
                command = [browser] if Path(browser).is_file() else shlex.split(browser)
            else:
                command = ["open" if sys.platform == "darwin" else "xdg-open"]
            if not command:
                raise ValueError
            if any("%s" in arg for arg in command[1:]):
                command = [command[0], *(arg.replace("%s", url) for arg in command[1:])]
            else:
                command.append(url)
            self.process = subprocess.Popen(
                command,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            print("Could not open the browser. Open the sign-in link above.", flush=True)

    def poll(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            self.process = None


def _hosted_authorization(url: str) -> bool:
    """Do not synthesize endpoints, scope, state, client ID, or PKCE parameters."""
    try:
        authorization = urlsplit(url)
        query = parse_qs(authorization.query, max_num_fields=32)
        redirect = urlsplit(_single(query, "redirect_uri"))
        return (
            authorization.scheme == "https"
            and redirect.scheme == "https"
            and redirect.hostname not in {None, "localhost", "127.0.0.1", "::1"}
            and redirect.path.endswith("/oauth/code/callback")
            and query.get("response_type") == ["code"]
            and query.get("code_challenge_method") == ["S256"]
            and bool(_single(query, "code_challenge"))
            and bool(_single(query, "state"))
        )
    except ValueError:
        return False


def _single(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    if len(values) != 1 or not values[0] or any(ord(c) < 33 or ord(c) > 126 for c in values[0]):
        raise ValueError("The login response is incomplete. Paste the complete code or URL.")
    return values[0]


def parse_login_response(
    value: str,
    expected_state: str | None,
    *,
    hosted_redirect: str | None = _HOSTED_CALLBACK,
) -> tuple[int | None, str]:
    """Return (callback port, callback path) or (None, manual code).

    Never include the supplied value in an exception: it contains a credential.
    Check state before any network request, so stale/other-session URLs cannot
    cancel this login or send their code to a different waiting Claude process.
    """
    value = value.strip()
    if len(value) > _MAX_INPUT or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("Invalid login response. Paste the complete code or callback URL.")
    if not expected_state:
        raise ValueError("Claude's login link is not ready yet. Wait for it, then try again.")
    port = None
    hosted = False
    if "://" in value:
        try:
            url = urlsplit(value)
            if url.username is not None or url.password is not None or url.fragment:
                raise ValueError
            if url.scheme == "https" and hosted_redirect:
                expected = urlsplit(hosted_redirect)
                if (url.scheme, url.netloc, url.path) != (
                    expected.scheme,
                    expected.netloc,
                    expected.path,
                ):
                    raise ValueError
                hosted = True
            elif (
                url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
                and url.port
                and url.path == "/callback"
            ):
                port = url.port
            else:
                raise ValueError
            query = parse_qs(url.query, keep_blank_values=True, max_num_fields=16)
            code, state = _single(query, "code"), _single(query, "state")
            if "#" in code or "#" in state:
                raise ValueError
        except ValueError:
            raise ValueError(
                "Paste the complete login code or callback URL from this login's browser tab."
            ) from None
    else:
        parts = value.split("#")
        if len(parts) != 2 or not all(parts) or any(c.isspace() for c in value):
            raise ValueError(
                "Paste the complete login code, including its # suffix, or callback URL."
            )
        code, state = parts
    if not secrets.compare_digest(state.encode(), expected_state.encode()):
        raise ValueError(
            "That response belongs to a different login. Use this login's browser tab."
        )
    if port is not None:
        return port, "/callback?" + urlencode({"code": code, "state": state})
    if hosted:
        return None, f"{code}#{state}"
    return None, value


def deliver_callback(port: int, path: str) -> None:
    # Pin to IPv4 loopback like Claude's listener. No DNS, environment proxies,
    # redirect following, shell commands, or secret-bearing process arguments.
    try:
        with contextlib.closing(
            http.client.HTTPConnection("127.0.0.1", port, timeout=35)
        ) as connection:
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status not in {200, 302}:
                raise RuntimeError("Claude rejected the callback. Check the login output.")
            # Claude also redirects on exchange errors. Only its process exit
            # status and stored credentials determine whether login succeeded.
    except (OSError, http.client.HTTPException):
        raise ValueError(
            "Could not reach the waiting login. Check the callback URL, or restart account add."
        ) from None


class _Output:
    def __init__(self, open_hosted: Callable[[str], None] | None = None) -> None:
        self.pending = ""
        self.expected_state: str | None = None
        self.ready = False
        self.prompt_visible = False
        self.open_hosted = open_hosted
        self.hosted_redirect: str | None = None

    def prompt(self) -> None:
        sys.stdout.write(_PROMPT)
        sys.stdout.flush()
        self.prompt_visible = True

    def newline(self) -> None:
        if self.prompt_visible:
            print(flush=True)
            self.prompt_visible = False

    def _line(self, line: str) -> None:
        plain = _ANSI.sub("", line)
        hosted_url = None
        for match in re.finditer(r"https?://[^\s<>]+", plain):
            try:
                url = urlsplit(match.group())
                query = parse_qs(url.query, max_num_fields=32)
                if query.get("response_type") == ["code"] and query.get("code_challenge"):
                    self.expected_state = _single(query, "state")
                    if _hosted_authorization(match.group()):
                        hosted_url = match.group()
                        self.hosted_redirect = _single(query, "redirect_uri")
            except ValueError:
                continue
        self.newline()
        sys.stdout.write(line)
        sys.stdout.flush()
        if hosted_url and self.open_hosted is not None:
            self.open_hosted(hosted_url)

    def feed(self, text: str, *, final: bool = False) -> None:
        self.pending += text
        while self.pending:
            prompt_at = self.pending.find(_CLAUDE_PROMPT)
            newline_at = self.pending.find("\n")
            if prompt_at >= 0 and (newline_at < 0 or prompt_at < newline_at):
                if prompt_at:
                    self._line(self.pending[:prompt_at])
                self.pending = self.pending[prompt_at + len(_CLAUDE_PROMPT) :]
                print(
                    "Sign in on Claude's hosted page, then paste its complete code here.\n"
                    "No code displayed? Paste the page's full callback URL. Ctrl-C cancels.",
                    flush=True,
                )
                self.ready = True
                self.prompt()
            elif newline_at >= 0:
                self._line(self.pending[: newline_at + 1])
                self.pending = self.pending[newline_at + 1 :]
            else:
                break
        if final and self.pending:
            self._line(self.pending)
            self.pending = ""


@contextlib.contextmanager
def _masked_terminal(descriptor: int) -> Iterator[bool]:
    if not os.isatty(descriptor):
        yield False
        return
    import termios

    original = termios.tcgetattr(descriptor)
    masked = original.copy()
    masked[6] = original[6].copy()
    masked[3] &= ~(termios.ECHO | termios.ICANON)
    masked[6][termios.VMIN] = 1
    masked[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, masked)
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        yield True
    finally:
        # Discard any unread pasted suffix if browser login finishes first. It
        # must not spill into the invoking shell after terminal echo is restored.
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, original)
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


class _Input:
    def __init__(self, masked: bool) -> None:
        self.value = bytearray()
        self.masked = masked
        self.escape = bytearray()
        self.pasting = False
        self.overflow = False

    def feed(self, char: bytes) -> str | None:
        if self.escape or char == b"\x1b":
            self.escape.extend(char)
            if self.escape in (b"\x1b[200~", b"\x1b[201~"):
                self.pasting = self.escape == b"\x1b[200~"
                self.escape.clear()
            elif len(self.escape) > 16 or (
                len(self.escape) > 2 and (char.isalpha() or char == b"~")
            ):
                self.escape.clear()
            return None
        if char == b"\x03":
            raise KeyboardInterrupt
        if not char or (char == b"\x04" and not self.value):
            raise EOFError
        if char in (b"\n", b"\r"):
            if self.pasting:
                return None
            value = self.value.decode("ascii", errors="replace")
            self.value.clear()
            if self.overflow:
                self.overflow = False
                raise ValueError("Login response is too long. Paste only the code or callback URL.")
            return value
        if char in (b"\x7f", b"\b", b"\x15"):
            count = len(self.value) if char == b"\x15" else min(1, len(self.value))
            if count:
                del self.value[-count:]
                if self.masked:
                    sys.stdout.write("\b \b" * count)
        elif char >= b" " and char != b"\x7f":
            if len(self.value) >= _MAX_INPUT:
                self.overflow = True
            else:
                self.value.extend(char)
                if self.masked:
                    sys.stdout.write("*")
        sys.stdout.flush()
        return None


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    # Signal only the login process, not other Claude agents, the invoking shell,
    # or a browser the opener may have started. Reap it before profile cleanup.
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        process.wait(timeout=3)


def run_login(command: list[str], *, env: dict[str, str]) -> int:
    """Run official Claude login with interruptible, secret-masked input."""
    try:
        input_fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        raise RuntimeError("Account login needs a terminal or stdin pipe.") from None
    browser = _HostedBrowser(env)
    output = _Output(browser.open)
    # `auth login` prints a hosted-code URL but opens a DIFFERENT URL that sends
    # the browser to localhost. Suppress only that child-process browser launch;
    # CAM opens the printed hosted URL with the original browser environment.
    # Always use this path: SSH detection misses container and IDE-forwarded
    # browsers. No account/profile/global browser setting is changed.
    noop = shutil.which("true", path=os.defpath)
    if noop is None:
        raise RuntimeError(
            "Cannot safely disable Claude's loopback browser launch: true not found."
        )
    login_env = dict(env, BROWSER=noop)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # Mask before spawning, including while the browser is opening. The child
    # has pipes, so only this process can read/echo input in the user's terminal.
    with (
        _masked_terminal(input_fd) as masked,
        subprocess.Popen(
            command,
            env=login_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
        ) as process,
    ):
        assert process.stdin is not None and process.stdout is not None
        stdout_fd = process.stdout.fileno()
        entry = _Input(masked)
        try:
            while True:
                browser.poll()
                readers = [stdout_fd]
                if output.ready:
                    readers.append(input_fd)
                readable, _, _ = select.select(readers, [], [], 0.1)
                if not readable and process.poll() is not None:
                    output.feed(decoder.decode(b"", final=True), final=True)
                    break
                if stdout_fd in readable:
                    chunk = os.read(stdout_fd, 4096)
                    if not chunk:
                        output.feed(decoder.decode(b"", final=True), final=True)
                        break
                    output.feed(decoder.decode(chunk))
                if input_fd in readable and process.poll() is None:
                    try:
                        value = entry.feed(os.read(input_fd, 1))
                        if value is None:
                            continue
                        output.newline()
                        if not value.strip():
                            output.prompt()
                            continue
                        port, response = parse_login_response(
                            value, output.expected_state, hosted_redirect=output.hosted_redirect
                        )
                        if port is None:
                            process.stdin.write((response + "\n").encode())
                            process.stdin.flush()
                        else:
                            print("Completing browser login…", flush=True)
                            deliver_callback(port, response)
                        output.ready = False
                    except ValueError as exc:
                        output.newline()
                        print(str(exc), flush=True)
                        output.prompt()
                    except EOFError:
                        raise RuntimeError("Claude login cancelled: input closed.") from None
            return process.wait(timeout=5)
        finally:
            output.newline()
            _stop(process)
            browser.poll()
