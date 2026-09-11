"""Run in Docker: verify real Claude auth status across synthetic native switches."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from test_native import logins

from claude_auth_manager.native import use_account

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
