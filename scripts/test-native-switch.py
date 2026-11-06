"""Run in Docker: verify real Claude auth status across synthetic native switches."""

import json
import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pexpect
from test_native import logins

from claude_auth_manager.native import active_native_sessions, use_account

with TemporaryDirectory() as directory:
    root = Path(directory)
    os.environ.update(
        HOME=directory,
        XDG_CONFIG_HOME=str(root / ".config"),
        XDG_STATE_HOME=str(root / ".local/state"),
    )
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    logins.__wrapped__(root)
    for name in ("one", "two", "one"):
        use_account(name)
        result = subprocess.run(
            ["/usr/local/bin/claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        document = json.loads(result.stdout)
        assert document.get("email") == name + "@example.com", document
        assert document.get("loggedIn") is True, document
    print("PASS: real Claude auth status follows native switching and switching back")
    # Hold a real Claude TUI open while changing the default for another launch.
    child = pexpect.spawn(
        "/usr/local/bin/claude",
        [],
        cwd=directory,
        encoding="utf-8",
        env={**os.environ, "TERM": "xterm-256color"},
        timeout=10,
    )
    try:
        deadline = time.monotonic() + 10
        while not active_native_sessions(root / ".claude") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert child.isalive() and active_native_sessions(root / ".claude")
        use_account("two")
        assert child.isalive(), "switch terminated the existing session"
        result = subprocess.run(
            ["/usr/local/bin/claude", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert json.loads(result.stdout).get("email") == "two@example.com"
        print("PASS: native switch succeeds with Claude running; new launch uses the new account")
    finally:
        child.close(force=True)
