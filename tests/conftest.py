from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_BIN_HOME", str(home / ".local" / "bin"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    return home


@pytest.fixture
def sample_models() -> list[dict[str, Any]]:
    return [
        {
            "id": "anthropic/claude-sonnet-4.6",
            "name": "Claude Sonnet 4.6",
            "description": "Fast agentic coding model",
            "context_length": 200_000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
        {
            "id": "anthropic/claude-opus-4.6",
            "name": "Claude Opus 4.6",
            "description": "Deep reasoning model",
            "context_length": 200_000,
            "pricing": {"prompt": "0.000005", "completion": "0.000025"},
        },
        {
            "id": "google/gemini-3.1-pro-preview",
            "name": "Gemini 3.1 Pro Preview",
            "description": "Multimodal reasoning and tools",
            "context_length": 1_000_000,
            "pricing": {"prompt": "0.000002", "completion": "0.000012"},
            "supported_parameters": ["tools", "tool_choice", "max_tokens"],
        },
        {
            "id": "qwen/qwen3-coder",
            "name": "Qwen3 Coder",
            "description": "Coding model",
            "context_length": 262_144,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["max_tokens"],
        },
    ]


@pytest.fixture
def managed_models(sample_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    openrouter = dict(sample_models[3])
    openrouter.update(provider="openrouter", credential="router-a")
    google = {
        "id": "gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "description": "Direct Google model",
        "supported_parameters": ["tools", "tool_choice"],
        "architecture": {"input_modalities": ["text", "image"]},
        "provider": "google",
        "credential": "google-a",
    }
    subscription = {
        "id": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "description": "Claude subscription",
        "context_length": 1_000_000,
        "supported_parameters": ["tools", "tool_choice"],
        "provider": "anthropic",
        "credential": "account-a",
        "credential_label": "a@example.com",
        "subscription": "max",
        "rate_limit_tier": "default_claude_max_20x",
    }
    return [subscription, openrouter, google]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
