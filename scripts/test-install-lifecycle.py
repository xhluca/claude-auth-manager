"""Exercise public downloads, update, and uninstall in disposable homes/tool stores."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_auth_manager import __version__

INSTALLER = "https://raw.githubusercontent.com/xhluca/claude-auth-manager/main/install.sh"
OLD_WHEEL = (
    "https://github.com/xhluca/claude-auth-manager/releases/download/"
    "v0.0.1/claude_auth_manager-0.0.1-py3-none-any.whl"
)


def run(command, env, root, **kwargs):
    result = subprocess.run(
        command, env=env, cwd=root, capture_output=True, text=True, timeout=180, **kwargs
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main():
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to exercise both installation paths")
    for mode in ("uv", "python", "release-fallback"):
        with TemporaryDirectory(prefix="cam-lifecycle-") as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            # No Claude account/network interaction is needed for --install-only.
            claude = binary / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n")
            claude.chmod(0o755)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CLAUDE_", "ANTHROPIC_", "UV_", "PIP_", "XDG_"))
                and key not in {"DBUS_SESSION_BUS_ADDRESS", "VIRTUAL_ENV", "PYTHONPATH"}
            }
            env.update(
                {
                    "HOME": str(root),
                    "XDG_CONFIG_HOME": str(root / ".config"),
                    "XDG_DATA_HOME": str(root / ".local/share"),
                    "XDG_STATE_HOME": str(root / ".local/state"),
                    "XDG_BIN_HOME": str(binary),
                    "CLAUDE_CONFIG_DIR": str(root / ".claude"),
                    "CLAUDE_AUTH_MANAGER_TOOL_DIR": str(root / "custom-tool"),
                    "UV_TOOL_DIR": str(root / "tools"),
                    "UV_TOOL_BIN_DIR": str(binary),
                    "UV_CACHE_DIR": str(root / "cache"),
                    "TMPDIR": str(root),
                    "PYTHON": sys.executable,
                    "PATH": os.pathsep.join(
                        [
                            str(binary),
                            *([str(Path(uv).parent)] if mode == "uv" else []),
                            "/usr/bin",
                            "/bin",
                        ]
                    ),
                }
            )
            script = run(["curl", "-fsSL", INSTALLER], env, root)
            if mode == "release-fallback":
                empty_index = root / "empty-index"
                empty_index.mkdir()
                env["CLAUDE_AUTH_MANAGER_PYPI_INDEX_URL"] = empty_index.as_uri()
            output = run(
                ["sh", "-s", "--", "--install-only", "--skip-claude-install"],
                env,
                root,
                input=script,
            )
            if mode == "release-fallback":
                assert "Verified release checksum" in output
                env.pop("CLAUDE_AUTH_MANAGER_PYPI_INDEX_URL")
            executable = str(binary / "cam")
            assert run([executable, "--version"], env, root).strip() == __version__
            if mode == "uv":
                run([uv, "tool", "install", "--force", OLD_WHEEL], env, root)
                assert run([executable, "--version"], env, root).strip() == "0.0.1"
            run([executable, "update"], env, root)
            assert run([executable, "--version"], env, root).strip() == __version__
            run([executable, "uninstall"], env, root)
            assert not (binary / "cam").exists()
            assert not (root / "custom-tool").exists()
            assert not (root / "tools/claude-auth-manager").exists()
            print(f"PASS: {mode} public install → update → uninstall", flush=True)


if __name__ == "__main__":
    main()
