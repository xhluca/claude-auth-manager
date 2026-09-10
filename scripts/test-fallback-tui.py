"""Run inside Docker: real Claude TUI, synthetic accounts/upstreams, no live credentials."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import pexpect
import pyte
from test_fallback import chain

from claude_auth_manager.paths import claude_settings_path
from claude_auth_manager.settings import save_fallbacks
from claude_auth_manager.storage import atomic_write_json, atomic_write_text


def main(first_failure=429):
    with TemporaryDirectory(prefix="cam-fallback-tui-") as directory:
        root = Path(directory)
        os.environ.update(
            {
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(root / ".config"),
                "XDG_CACHE_HOME": str(root / ".cache"),
                "XDG_STATE_HOME": str(root / ".local/state"),
                "CLAUDE_CONFIG_DIR": str(root / ".claude"),
                "TERM": "xterm-256color",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        for key in list(os.environ):
            if key.startswith("ANTHROPIC_") or key in {
                "CLAUDECODE",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
            }:
                os.environ.pop(key)
        fixture = chain.__wrapped__(root)
        router, upstream, ids, _now = next(fixture)
        try:
            # Ranked alternatives with circular links must still complete the job.
            save_fallbacks({ids[0]: [ids[1], ids[2]], ids[1]: [ids[0]], ids[2]: [ids[0], ids[3]]})
            # Both Claude subscriptions reject; an OpenRouter model takes over.
            upstream.faults.update(
                {"test-provider-secret-max": first_failure, "test-provider-secret-personal": 529}
            )
            settings = json.loads(claude_settings_path().read_text())
            settings["env"].update(
                {
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{router.server_port}",
                    "ANTHROPIC_AUTH_TOKEN": "local-test",
                    "ANTHROPIC_CUSTOM_HEADERS": "X-Claude-Auth-Manager-Token: local-test",
                }
            )
            atomic_write_json(claude_settings_path(), settings)
            atomic_write_json(
                root / ".claude.json",
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "projects": {
                        str(root): {
                            "hasTrustDialogAccepted": True,
                            "allowedTools": [],
                            "mcpServers": {},
                            "hasCompletedProjectOnboarding": True,
                        }
                    },
                },
            )
            atomic_write_json(
                root / ".claude" / ".claude.json",
                json.loads((root / ".claude.json").read_text()),
            )
            atomic_write_text(root / "probe.txt", "test fixture\n")
            child = pexpect.spawn(
                "/usr/local/bin/claude",
                [
                    "--model",
                    ids[0],
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    "Glob",
                    "--allowedTools",
                    "Glob",
                ],
                cwd=str(root),
                encoding="utf-8",
                dimensions=(40, 180),
                timeout=45,
            )
            screen = pyte.Screen(180, 40)
            terminal = pyte.Stream(screen)
            transcript = []

            def wait_for(needle, timeout=45):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        chunk = child.read_nonblocking(16384, timeout=0.2)
                        transcript.append(chunk)
                        terminal.feed(chunk)
                    except pexpect.TIMEOUT:
                        pass
                    display = "\n".join(screen.display)
                    if needle in display:
                        return display
                raise AssertionError(f"TUI did not show {needle!r}:\n" + "\n".join(screen.display))

            try:
                # Trust is local to this disposable container home.
                wait_for("trust", timeout=20)
                child.send("\r")
            except AssertionError:
                pass
            try:
                child.send("Use Glob to find probe.txt and then confirm completion.\r")
                display = wait_for("CAM_JOB_CONTINUED_OK")
                assert "CAM fallback active" in display, display
                assert any("tool_result" in json.dumps(call[1]) for call in upstream.calls)
                assert {call[0] for call in upstream.calls} >= {
                    "test-provider-secret-max",
                    "test-provider-secret-personal",
                    "test-provider-secret-router",
                }
                print(
                    "PASS: Claude TUI displayed fallback and completed a real Glob tool round-trip."
                )
                saved = json.loads(claude_settings_path().read_text())
                assert any(
                    "Fallback active" in option.get("description", "")
                    for option in saved["modelPicker"]["options"]
                ), "Router did not save the active fallback description"
                # Allow Claude's debounced settings watcher to observe the write.
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with suppress(pexpect.TIMEOUT):
                        terminal.feed(child.read_nonblocking(16384, timeout=0.2))
                child.send("/model\r")
                display = wait_for("Fallback active")
                assert "GLM" in display or "glm" in display
                print("PASS: /model picker refreshed its active fallback in the same process.")
            finally:
                child.close(force=True)
        finally:
            fixture.close()


if __name__ == "__main__":
    main("late-error" if "--late" in sys.argv else 429)
