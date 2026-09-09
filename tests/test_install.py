from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from claude_auth_manager import __version__

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["pypi", "fallback", "bad-checksum"])
def test_piped_installer_registry_and_verified_fallback(tmp_path, monkeypatch, mode):
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls.jsonl"
    uv = binary / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['CAM_TEST_LOG'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if '--default-index' in sys.argv and os.environ['CAM_TEST_MODE'] != 'pypi': sys.exit(1)\n"
        "p = pathlib.Path(os.environ['UV_TOOL_BIN_DIR']) / 'claude-auth-manager'\n"
        "p.write_text('#!/bin/sh\\nexit 0\\n'); p.chmod(0o755)\n"
    )
    uv.chmod(0o755)
    claude = binary / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    wheel = tmp_path / f"claude_auth_manager-{__version__}-py3-none-any.whl"
    wheel.write_bytes(b"synthetic package")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    script = (ROOT / "install.sh").read_text()
    script = re.sub(r'wheel_sha256="[a-f0-9]+"', f'wheel_sha256="{digest}"', script)
    if mode == "bad-checksum":
        wheel.write_bytes(b"tampered")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for key, value in {
        "PATH": str(binary) + os.pathsep + os.defpath,
        "UV_TOOL_BIN_DIR": str(binary),
        "CAM_TEST_LOG": str(log),
        "CAM_TEST_MODE": mode,
        "CLAUDE_AUTH_MANAGER_INSTALL_BASE_URL": tmp_path.as_uri(),
        "TMPDIR": str(scratch),
    }.items():
        monkeypatch.setenv(key, value)
    result = subprocess.run(
        ["sh", "-s", "--", "--install-only", "--skip-claude-install"],
        input=script,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "--default-index" in calls[0]
    if mode == "bad-checksum":
        assert result.returncode != 0 and "checksum mismatch" in result.stderr
        assert len(calls) == 1
    else:
        assert result.returncode == 0, result.stderr
        assert len(calls) == (2 if mode == "fallback" else 1)
    assert not list(scratch.iterdir())


def test_shell_scripts_are_syntactically_valid() -> None:
    subprocess.run(["sh", "-n", str(ROOT / "install.sh")], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "live-docker-check.sh")], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "build-release.sh")], check=True)


def test_installer_help_does_not_require_network() -> None:
    result = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "never accepts credentials as command-line arguments" in result.stdout
    assert "--install-only" in result.stdout
    assert "account add --current" in (ROOT / "install.sh").read_text(encoding="utf-8")


def test_installer_release_matches_package_version_and_has_checksum() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert f'package_version="{__version__}"' in installer
    assert 'wheel_sha256="TO_BE_REPLACED"' not in installer


def test_uv_installs_use_copy_mode_to_avoid_cross_filesystem_warnings() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    commands = [line.strip() for line in installer.splitlines() if "uv tool install" in line]

    assert commands
    assert all("--link-mode copy" in command for command in commands)


def test_public_installer_supports_pypi_and_verified_release_fallback() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "xhluca.github.io" not in installer
    assert "github.com/xhluca/claude-auth-manager/releases/download/" in installer
    assert "install_target=${wheel_path:-$local_source}" in installer
    assert "prepare_fallback_wheel" in installer
    assert "verify_wheel" in installer
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "curl -fsSL https://raw.githubusercontent.com/xhluca/claude-auth-manager/" in readme
    assert "uv tool install claude-auth-manager" in readme
    assert "cam update" in readme and "cam uninstall" in readme


def test_readme_documents_multi_account_multi_key_routes_and_reset() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "cam account add" in readme
    assert "cam key add personal --provider openrouter" in readme
    assert "cam key add lab --provider openrouter" in readme
    assert "cam key add google-work --provider google" in readme
    assert "cam/openrouter/personal/qwen/qwen3-coder" in readme
    assert "concurrently" in readme
    assert "cam reset" in readme


def test_readme_command_reference_covers_every_public_option() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    reference = readme.split("## Commands", 1)[1].split("## Security and behavior", 1)[0]
    for option in (
        "--help",
        "--version",
        "--account",
        "--key",
        "--model",
        "--route",
        "--config",
        "--check-confirmation",
        "--json",
        "--current",
        "--token",
        "--token-stdin",
        "--name",
        "--provider",
        "-p",
        "--label",
        "--key-path",
        "--key-stdin",
        "--no-validate",
        "--tools",
        "--offline",
        "--port",
        "--yes",
        "-y",
        "--host",
    ):
        assert f"`{option}" in reference, option


def test_live_docker_check_uses_read_only_secret_mounts_and_all_providers() -> None:
    script = (ROOT / "scripts" / "live-docker-check.sh").read_text(encoding="utf-8")
    assert "src=$openrouter_key,dst=/run/secrets/openrouter,readonly" in script
    assert "src=$google_key,dst=/run/secrets/google,readonly" in script
    assert "cam account add --current" in script
    assert "cam key add router --provider openrouter --key-path /run/secrets/openrouter" in script
    assert "cam key add google --provider google --key-path /run/secrets/google" in script
    assert "cam check --json" in script
    assert "cam list --route --json" in script
    assert "cam doctor" not in script
    assert "cam routes" not in script
    assert "PARALLEL_PROVIDER_ROUTES_OK" in script
    assert "RESET_AND_NATIVE_CREDENTIAL_PRESERVATION_OK" in script
