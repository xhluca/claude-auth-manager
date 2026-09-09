from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claude_auth_manager import uninstall


@pytest.mark.parametrize("manager", ["uv", "pipx"])
def test_uninstall_uses_only_the_manager_owning_this_process(tmp_path, monkeypatch, manager):
    root = tmp_path / manager
    monkeypatch.setattr(uninstall.sys, "prefix", str(root / uninstall.PACKAGE_NAME))
    monkeypatch.setattr(uninstall.shutil, "which", lambda name: f"/bin/{name}")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        location = root if Path(command[0]).name == manager else tmp_path / "unrelated"
        return subprocess.CompletedProcess(command, 0, stdout=str(location), stderr="")

    monkeypatch.setattr(uninstall.subprocess, "run", run)
    assert uninstall.remove_installed_package()
    expected = [f"/bin/{manager}"] + (["tool"] if manager == "uv" else [])
    assert commands[-1] == [*expected, "uninstall", uninstall.PACKAGE_NAME]
    assert sum("uninstall" in command for command in commands) == 1


def test_development_uninstall_does_not_remove_another_uv_install(tmp_path, monkeypatch):
    monkeypatch.setattr(uninstall.sys, "prefix", str(tmp_path / "development"))
    monkeypatch.setattr(uninstall.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=str(tmp_path / "uv"))

    monkeypatch.setattr(uninstall.subprocess, "run", run)
    assert not uninstall.remove_installed_package()
    assert commands == [["/bin/uv", "tool", "dir"]]


def test_custom_private_venv_uninstall_preserves_unrelated_files(isolated_home, monkeypatch):
    tool = isolated_home / "custom-tool"
    (tool / "bin").mkdir(parents=True)
    (tool / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (tool / "bin/cam").write_text("fixture")
    bin_dir = isolated_home / ".local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "cam").symlink_to(tool / "bin/cam")
    unrelated = bin_dir / "claude-auth-manager"
    unrelated.write_text("keep")
    monkeypatch.setenv("CLAUDE_AUTH_MANAGER_TOOL_DIR", str(tool))
    monkeypatch.setattr(uninstall.sys, "prefix", str(tool))
    monkeypatch.setattr(uninstall.shutil, "which", lambda _name: None)
    assert uninstall.remove_installed_package()
    assert not tool.exists() and not (bin_dir / "cam").is_symlink()
    assert unrelated.read_text() == "keep"


def test_uninstall_refuses_a_broad_or_unmarked_directory(isolated_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTH_MANAGER_TOOL_DIR", str(isolated_home))
    monkeypatch.setattr(uninstall.sys, "prefix", str(isolated_home))
    (isolated_home / "pyvenv.cfg").write_text("fixture")
    assert not uninstall._remove_fallback_install()
    assert isolated_home.exists()
