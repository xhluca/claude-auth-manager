"""Best-effort removal of the installed package after integration reset."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_NAME = "claude-auth-manager"


def _fallback_tool_dir() -> Path:
    configured = os.environ.get("CLAUDE_AUTH_MANAGER_TOOL_DIR")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return data_home / PACKAGE_NAME / "tool"


def _bin_dir() -> Path:
    return Path(os.environ.get("XDG_BIN_HOME", Path.home() / ".local" / "bin"))


def _remove_fallback_install() -> bool:
    tool_dir = _fallback_tool_dir()
    # Virtualenv Python executables often resolve to the shared system Python.
    # sys.prefix identifies the environment actually executing this command.
    if Path(sys.prefix).resolve() != tool_dir.resolve():
        return False
    protected = {
        Path("/"),
        Path.home().resolve(),
        Path.cwd().resolve(),
        *Path.cwd().resolve().parents,
    }
    if (
        tool_dir.is_symlink()
        or tool_dir.resolve() in protected
        or not (tool_dir / "pyvenv.cfg").is_file()
    ):
        return False
    for command in ("claude-auth-manager", "cam"):
        link = _bin_dir() / command
        if link.is_symlink() and tool_dir.resolve() in link.resolve().parents:
            link.unlink()
    if tool_dir.exists():
        shutil.rmtree(tool_dir)
    parent = tool_dir.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    return True


def remove_installed_package() -> bool:
    uv = shutil.which("uv")
    if uv:
        location = subprocess.run(
            [uv, "tool", "dir"],
            check=False,
            capture_output=True,
            text=True,
        )
        if (
            location.returncode == 0
            and location.stdout.strip()
            and Path(sys.prefix).resolve()
            == (Path(location.stdout.strip()) / PACKAGE_NAME).resolve()
        ):
            removed = subprocess.run([uv, "tool", "uninstall", PACKAGE_NAME], check=False)
            return removed.returncode == 0
    pipx = shutil.which("pipx")
    if pipx:
        location = subprocess.run(
            [pipx, "environment", "--value", "PIPX_LOCAL_VENVS"],
            check=False,
            capture_output=True,
            text=True,
        )
        if (
            location.returncode == 0
            and location.stdout.strip()
            and Path(sys.prefix).resolve()
            == (Path(location.stdout.strip()) / PACKAGE_NAME).resolve()
        ):
            removed = subprocess.run([pipx, "uninstall", PACKAGE_NAME], check=False)
            return removed.returncode == 0
    # Plain pip and development installs are intentionally not guessed: removing
    # from an arbitrary Python environment can damage unrelated applications.
    return _remove_fallback_install()
