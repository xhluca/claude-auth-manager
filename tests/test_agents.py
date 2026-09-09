from __future__ import annotations

import json

from claude_auth_manager.agents import (
    MANAGED_MARKER,
    agent_name,
    remove_managed_agents,
    rewrite_agent_input,
    sync_managed_agents,
)
from claude_auth_manager.models import claude_model, managed_model
from claude_auth_manager.paths import agent_manifest_path, claude_agents_dir


def test_managed_agents_expose_each_exact_openrouter_favorite(isolated_home, sample_models) -> None:
    selected = sample_models[2:]

    routes = sync_managed_agents(selected)

    assert routes == {
        agent_name(managed_model(model)): f"cam/openrouter/{model['id']}" for model in selected
    }
    manifest = json.loads(agent_manifest_path().read_text())
    assert set(manifest["agents"]) == set(routes)
    for name, route in routes.items():
        path = claude_agents_dir() / f"{name}.md"
        document = path.read_text()
        assert MANAGED_MARKER in document
        assert f"model: {json.dumps(route)}" in document
        assert "Do not pass the Agent model parameter" in document


def test_agent_hook_removes_native_alias_override_only_for_managed_agent(
    isolated_home, sample_models
) -> None:
    selected = sample_models[2:]
    routes = sync_managed_agents(selected)
    managed = next(iter(routes))
    original = {
        "description": "delegate",
        "prompt": "check it",
        "subagent_type": managed,
        "model": "sonnet",
        "run_in_background": True,
    }

    result = rewrite_agent_input({"tool_name": "Agent", "tool_input": original})

    assert result is not None
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    assert output["updatedInput"] == {
        key: value for key, value in original.items() if key != "model"
    }
    assert (
        rewrite_agent_input(
            {
                "tool_name": "Agent",
                "tool_input": {**original, "subagent_type": "general-purpose"},
            }
        )
        is None
    )
    assert rewrite_agent_input({"tool_name": "Read", "tool_input": original}) is None


def test_reselection_replaces_only_cam_owned_agent_files(isolated_home, sample_models) -> None:
    sync_managed_agents(sample_models[2:])
    unrelated = claude_agents_dir() / "user-agent.md"
    unrelated.write_text("user owned")

    sync_managed_agents(sample_models[3:])

    assert unrelated.read_text() == "user owned"
    assert not (claude_agents_dir() / f"{agent_name(managed_model(sample_models[2]))}.md").exists()
    assert (claude_agents_dir() / f"{agent_name(managed_model(sample_models[3]))}.md").exists()

    remove_managed_agents()
    assert unrelated.exists()
    assert not agent_manifest_path().exists()


def test_same_upstream_model_on_two_keys_creates_two_exact_subagents(
    isolated_home, sample_models
) -> None:
    first = dict(sample_models[3], provider="openrouter", credential="key-a")
    second = dict(sample_models[3], provider="openrouter", credential="key-b")

    routes = sync_managed_agents([first, second])

    assert len(routes) == 2
    assert set(routes.values()) == {
        "cam/openrouter/key-a/qwen/qwen3-coder",
        "cam/openrouter/key-b/qwen/qwen3-coder",
    }
    assert len(list(claude_agents_dir().glob("cam-*.md"))) == 2


def test_generated_claude_agent_selects_extended_context(isolated_home, managed_models) -> None:
    model = managed_models[0]
    routes = sync_managed_agents([model])
    name = agent_name(managed_model(model))
    document = (claude_agents_dir() / f"{name}.md").read_text()

    assert routes[name] == claude_model(model)
    assert f"model: {json.dumps(claude_model(model))}" in document
    assert "claude-sonnet-4-6[1m]" in document
    assert "Sonnet 4.6 (1M context)" in document
