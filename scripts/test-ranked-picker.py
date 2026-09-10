"""Exercise real curses ranking hotkeys in a PTY with synthetic model metadata."""

import json
import os
import sys

import pexpect


def main():
    code = """
import json
from claude_auth_manager.picker import choose_models
models = [{"id": name, "name": name, "credential": "test-key"}
          for name in ("alpha", "beta", "gamma")]
links = {}
result = choose_models(models, ["alpha", "beta", "gamma"], fallbacks=links)
print("RESULT " + json.dumps({"selected": result, "fallbacks": links}), flush=True)
"""
    child = pexpect.spawn(
        sys.executable,
        ["-c", code],
        encoding="utf-8",
        timeout=10,
        env={**os.environ, "TERM": "xterm-256color"},
        dimensions=(24, 100),
    )
    try:
        child.expect("Search:")
        child.send("\r\x06")
        child.expect("Fallback for")
        child.send("beta\r")
        child.send("\x7f" * 4 + "gamma\r")
        child.send("\t\x04\x13s")
        child.expect(r"RESULT ([^\r\n]+)")
        result = json.loads(child.match.group(1))
        assert result == {
            "selected": ["alpha", "beta", "gamma"],
            "fallbacks": {"alpha": ["gamma", "beta"]},
        }, result
        child.expect(pexpect.EOF)
        print("PASS: real curses search, add, ranked view, reorder, apply, and save")
    finally:
        child.close(force=True)


if __name__ == "__main__":
    main()
