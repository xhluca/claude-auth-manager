from __future__ import annotations

import subprocess
from pathlib import Path

from claude_auth_manager import __version__

ROOT = Path(__file__).resolve().parents[1]


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


def test_private_installer_does_not_use_anonymous_downloads() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "xhluca.github.io" not in installer
    assert "github.com/xhluca/claude-auth-manager" not in installer
    assert "install_target=${wheel_path:-$local_source}" in installer
    assert "run install.sh from a claude-auth-manager source checkout" in installer
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "gh release download v0.0.1 --repo xhluca/claude-auth-manager" in readme
    assert "gh auth login" in readme
    assert not (ROOT / ".github/workflows/publish.yml").exists()


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
