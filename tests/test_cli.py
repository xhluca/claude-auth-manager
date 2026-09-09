from __future__ import annotations

import argparse
import io
import json
from argparse import Namespace
from pathlib import Path

import pytest

from claude_auth_manager import __version__, cli, registry
from claude_auth_manager.check import ToolProbeResult
from claude_auth_manager.models import managed_model
from claude_auth_manager.paths import account_credential_path, claude_settings_path
from claude_auth_manager.registry import add_account_token, add_key, list_keys
from claude_auth_manager.settings import favorite_models, load_preferences, save_preferences
from claude_auth_manager.storage import atomic_write_json


def test_version_and_primary_commands_parse() -> None:
    assert __version__ == "0.0.2"
    assert (
        cli.parser().parse_args(["key", "add", "work", "--provider", "google"]).provider == "google"
    )
    assert (
        cli.parser()
        .parse_args(["key", "add", "work", "--provider", "google", "--key", "secret-value"])
        .key
        == "secret-value"
    )
    assert cli.parser().parse_args(
        ["key", "add", "work", "--provider", "google", "--key-path", "/run/key"]
    ).key_path == Path("/run/key")
    assert cli.parser().parse_args(["select", "google/work/gemini-3.8-flash"]).routes
    model_list = cli.parser().parse_args(
        ["list", "--model", "--account", "one@example.com", "sonnet", "--json"]
    )
    assert model_list.queries == ["sonnet"]
    assert model_list.account == "one@example.com"
    assert model_list.models_only is True
    assert cli.parser().parse_args(["list", "--account"]).account == ""
    assert cli.parser().parse_args(
        ["select", "--account", "one@example.com", "--account", "Work"]
    ).accounts == ["one@example.com", "Work"]
    assert cli.parser().parse_args(["update"]).command == "update"
    assert cli.parser().parse_args(["list"]).command == "list"
    assert cli.parser().parse_args(["list", "--route"]).routes_only is True
    assert cli.parser().parse_args(["list", "--config"]).config_only is True
    assert cli.parser().parse_args(["check"]).route is None
    assert cli.parser().parse_args(["account", "add"]).current is False
    assert cli.parser().parse_args(["account", "add", "--current"]).current is True
    assert cli.parser().parse_args(["account", "add", "--token"]).token is True
    assert cli.parser().parse_args(["account", "add", "--token-stdin"]).token_stdin is True
    assert cli.parser().parse_args(["account", "add", "--name", "work"]).name == "work"


def test_every_public_cli_argument_and_command_has_help() -> None:
    visited: set[int] = set()

    def check(command_parser: argparse.ArgumentParser) -> None:
        if id(command_parser) in visited:
            return
        visited.add(id(command_parser))
        assert command_parser.description
        for action in command_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    check(child)
                continue
            if action.dest != "help":
                assert action.help not in {None, argparse.SUPPRESS}, action.dest

    check(cli.parser())


@pytest.mark.parametrize(
    ("arguments", "usage"),
    [
        ([], "usage: cam [-h] [--version] COMMAND ..."),
        (["account"], "usage: cam account [-h] ACTION ..."),
        (["key"], "usage: cam key [-h] ACTION ..."),
    ],
)
def test_bare_command_groups_show_help_without_an_error(arguments, usage, capsys) -> None:
    assert cli.main(arguments) == 0
    output = capsys.readouterr()
    assert usage in output.out
    assert "error:" not in output.out
    assert output.err == ""


@pytest.mark.parametrize(
    "command",
    ["setup", "doctor", "routes", "config", "fetch", "upgrade", "claude"],
)
def test_removed_compatibility_commands_do_not_parse(command) -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args([command])


@pytest.mark.parametrize("kind", ["account", "key"])
def test_removed_nested_list_commands_do_not_parse(kind) -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args([kind, "list"])


def test_removed_import_flag_does_not_parse() -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["key", "add", "old", "--provider", "openrouter", "--import"])


def test_account_add_defaults_to_official_login_and_current_is_opt_in(monkeypatch, capsys) -> None:
    logged_in: list[str | None] = []
    imported: list[str | None] = []
    monkeypatch.setattr(
        cli,
        "login_account",
        lambda name: (
            logged_in.append(name) or {"id": "person-example-com", "email": "person@example.com"}
        ),
    )
    monkeypatch.setattr(
        cli,
        "add_current_account",
        lambda name: (
            imported.append(name) or {"id": "current-example-com", "email": "current@example.com"}
        ),
    )
    monkeypatch.setattr(cli, "assert_private_files", lambda: None)
    bootstrapped: list[str] = []
    monkeypatch.setattr(
        cli,
        "_bootstrap_account",
        lambda account_id: bootstrapped.append(account_id),
    )

    assert cli.command_account(cli.parser().parse_args(["account", "add"])) == 0
    assert (
        cli.command_account(
            cli.parser().parse_args(["account", "add", "--current", "--name", "work"])
        )
        == 0
    )

    assert logged_in == [None]
    assert imported == ["work"]
    assert bootstrapped == ["person-example-com", "current-example-com"]
    output = capsys.readouterr().out
    assert "person-example-com" in output
    assert "current-example-com" in output


def test_account_login_interrupt_is_a_clean_cli_exit(monkeypatch, capsys) -> None:
    def cancel(_name):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "login_account", cancel)
    assert cli.main(["account", "add"]) == 130
    assert capsys.readouterr().err == "Cancelled.\n"


def test_relogin_reports_update_instead_of_duplicate_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "login_account",
        lambda name: {
            "id": "person-example-com",
            "email": "person@example.com",
            "updated": True,
        },
    )
    monkeypatch.setattr(cli, "_bootstrap_account", lambda account_id: False)
    monkeypatch.setattr(cli, "assert_private_files", lambda: None)
    assert cli.main(["account", "add"]) == 0
    output = capsys.readouterr()
    assert "Updated Claude subscription" in output.out
    assert not output.err


def test_account_add_token_uses_secure_input_and_needs_no_name(monkeypatch, capsys) -> None:
    reads: list[bool] = []
    saved: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        cli,
        "_read_secret",
        lambda _label, stdin: reads.append(stdin) or "setup-token-that-is-long-enough",
    )
    monkeypatch.setattr(
        cli,
        "add_account_token",
        lambda name, token: (
            saved.append((name, token)) or {"id": "token-0123456789ab", "email": None}
        ),
    )
    monkeypatch.setattr(cli, "assert_private_files", lambda: None)
    monkeypatch.setattr(cli, "_bootstrap_account", lambda _account_id: False)

    assert cli.command_account(cli.parser().parse_args(["account", "add", "--token"])) == 0
    assert (
        cli.command_account(
            cli.parser().parse_args(["account", "add", "--token-stdin", "--name", "automation"])
        )
        == 0
    )

    assert reads == [False, True]
    assert saved == [
        (None, "setup-token-that-is-long-enough"),
        ("automation", "setup-token-that-is-long-enough"),
    ]
    assert "token-0123456789ab" in capsys.readouterr().out


def test_top_level_list_combines_accounts_and_keys_with_filters(isolated_home, capsys) -> None:
    add_account_token("manual", "setup-token-that-is-long-enough", email="person@example.com")
    add_key("openrouter", "work", "openrouter-private-key", label="Work credits")

    assert cli.main(["list", "--json"]) == 0
    combined = json.loads(capsys.readouterr().out)
    assert [(entry["type"], entry["id"]) for entry in combined] == [
        ("account", "manual"),
        ("key", "work"),
    ]
    assert "setup-token-that-is-long-enough" not in json.dumps(combined)
    assert "openrouter-private-key" not in json.dumps(combined)

    assert cli.main(["list", "--account", "--json"]) == 0
    accounts = json.loads(capsys.readouterr().out)
    assert [entry["type"] for entry in accounts] == ["account"]

    assert cli.main(["list", "--key", "--json"]) == 0
    keys = json.loads(capsys.readouterr().out)
    assert [entry["type"] for entry in keys] == ["key"]


def test_list_models_is_static_searchable_and_source_scoped(
    isolated_home, managed_models, monkeypatch, capsys
) -> None:
    first = managed_models[0]
    second = dict(first, credential="account-b", credential_label="b@example.com")
    openrouter = managed_models[1]
    google = managed_models[2]
    models = [first, second, openrouter, google]
    monkeypatch.setattr(cli, "refresh_select_catalogs", lambda: models)
    monkeypatch.setattr(
        cli,
        "list_keys",
        lambda: [
            {"id": "router-a", "label": "Personal router", "provider": "openrouter"},
            {"id": "google-a", "label": "Lab Gemini", "provider": "google"},
        ],
    )

    assert (
        cli.main(
            [
                "list",
                "--model",
                "sonnet",
                "--account",
                "a@example.com",
                "--json",
            ]
        )
        == 0
    )
    account_models = json.loads(capsys.readouterr().out)
    assert len(account_models) == 1
    assert account_models[0]["route"] == managed_model(first)
    assert {key: value for key, value in account_models[0].items() if key != "route"} == first

    assert cli.main(["list", "--model", "qwen", "--key", "Personal router", "--json"]) == 0
    key_models = json.loads(capsys.readouterr().out)
    assert [model["route"] for model in key_models] == [managed_model(openrouter)]

    assert cli.main(["list", "--model", "--provider", "google", "--json"]) == 0
    provider_models = json.loads(capsys.readouterr().out)
    assert [model["route"] for model in provider_models] == [managed_model(google)]

    assert cli.main(["list", "--model", "--account", "--json"]) == 0
    subscriptions = json.loads(capsys.readouterr().out)
    assert [model["route"] for model in subscriptions] == [
        managed_model(first),
        managed_model(second),
    ]


def test_list_models_offline_uses_saved_catalogs(
    isolated_home, managed_models, monkeypatch
) -> None:
    loaded: list[bool] = []
    monkeypatch.setattr(
        cli,
        "load_all_catalogs",
        lambda: loaded.append(True) or managed_models,
    )
    monkeypatch.setattr(
        cli,
        "refresh_select_catalogs",
        lambda: (_ for _ in ()).throw(AssertionError("must stay offline")),
    )

    assert cli.main(["list", "--model", "--offline", "--json"]) == 0
    assert loaded == [True]


def test_list_model_filters_require_model_view(isolated_home, capsys) -> None:
    assert cli.main(["list", "sonnet"]) == 1
    assert "require --model" in capsys.readouterr().err

    assert cli.main(["list", "--account", "person@example.com"]) == 1
    assert "ACCOUNT value requires --model" in capsys.readouterr().err


def test_table_columns_align_without_terminal_tab_stops(capsys) -> None:
    cli._print_table(
        ("TYPE", "ID", "LABEL", "SOURCE"),
        [("account", "a", "ZZ", "SRC"), ("account", "much-longer", "YY", "SRC")],
    )
    header, short, long = capsys.readouterr().out.splitlines()
    assert header.index("ID") == short.index("a", len("account")) == long.index("much-longer")
    assert header.index("LABEL") == short.index("ZZ") == long.index("YY")
    assert header.index("SOURCE") == short.index("SRC") == long.index("SRC")


def test_list_migrates_slugged_email_ids_and_rewrites_routes(
    isolated_home, monkeypatch, capsys
) -> None:
    old = "person-example-com"
    new = "person@example.com"
    registry.save_registry(
        {
            "version": registry.REGISTRY_VERSION,
            "accounts": {
                old: {
                    "label": new,
                    "email": new,
                    "source": "managed",
                    "subscription": "max",
                }
            },
            "keys": {},
        }
    )
    atomic_write_json(
        account_credential_path(old),
        {"claudeAiOauth": {"accessToken": "managed-access-token-long"}},
    )
    model = {
        "id": "claude-sonnet-5",
        "name": "Sonnet 5",
        "provider": "anthropic",
        "credential": old,
        "credential_label": new,
    }
    save_preferences([model], managed_model(model))
    starts: list[int] = []
    monkeypatch.setattr(cli, "has_native_login", lambda: True)
    monkeypatch.setattr(cli, "start_service", lambda port: starts.append(port))

    assert cli.main(["list", "--account"]) == 0

    output = capsys.readouterr().out
    assert new in output
    assert old not in output
    assert favorite_models()[0]["credential"] == new
    assert f"cam/anthropic/{new}/" in str(load_preferences()["default_model"])
    settings = json.loads(claude_settings_path().read_text())
    assert f"cam/anthropic/{new}/" in settings["model"]
    assert starts == [9427]


def test_list_merges_route_and_config_views(isolated_home, managed_models, capsys) -> None:
    save_preferences(managed_models[:1], managed_model(managed_models[0]), port=9555)

    assert cli.main(["list", "--route", "--json"]) == 0
    routes = json.loads(capsys.readouterr().out)
    assert [managed_model(route) for route in routes] == [managed_model(managed_models[0])]

    assert cli.main(["list", "--config", "--check-confirmation", "never", "--json"]) == 0
    configuration = json.loads(capsys.readouterr().out)
    assert configuration == {
        "default_model": managed_model(managed_models[0]),
        "router_port": 9555,
        "check_confirmation": "never",
        "routes": [managed_model(managed_models[0])],
        "fallbacks": {},
        "fallback_state": {},
    }


def test_list_rejects_config_setter_without_config_view(isolated_home, capsys) -> None:
    assert cli.main(["list", "--check-confirmation", "never"]) == 1
    assert "requires --config" in capsys.readouterr().err


def test_first_account_add_bootstraps_default_route(isolated_home, monkeypatch, capsys) -> None:
    entry = add_account_token("first", "setup-token-that-is-long-enough")
    configured: list[tuple[list[dict], int]] = []
    monkeypatch.setattr(
        cli,
        "_configure",
        lambda models, port: configured.append((models, port)),
    )

    assert cli._bootstrap_account(str(entry["id"])) is True
    assert configured[0][0][0]["id"] == "claude-sonnet-5"
    assert configured[0][0][0]["credential"] == "first"
    assert configured[0][1] == 9427
    assert "Initialized default route" in capsys.readouterr().out


def test_key_add_reads_stdin_validates_and_indexes(isolated_home, monkeypatch, capsys) -> None:
    checked: list[tuple[str, str]] = []
    indexed: list[str] = []
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("private-google-key\n"))
    monkeypatch.setattr(
        cli,
        "_validate_provider_key",
        lambda provider, secret: checked.append((provider, secret)),
    )
    monkeypatch.setattr(
        cli,
        "refresh_key_catalog",
        lambda key_id: indexed.append(key_id) or [{"id": "gemini-3.8-flash"}],
    )

    result = cli.command_key_add(
        "google", "work", label="Google work", key_stdin=True, no_validate=False
    )

    assert result == 0
    assert checked == [("google", "private-google-key")]
    assert indexed == ["work"]
    assert list_keys() == [{"id": "work", "provider": "google", "label": "Google work"}]
    assert "private-google-key" not in capsys.readouterr().out


def test_key_add_can_store_without_network_validation(isolated_home, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("private-google-key\n"))
    monkeypatch.setattr(
        cli,
        "_validate_provider_key",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not validate")),
    )
    monkeypatch.setattr(
        cli,
        "refresh_key_catalog",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not index")),
    )

    result = cli.command_key_add("google", "offline", label=None, key_stdin=True, no_validate=True)

    assert result == 0
    assert list_keys("google")[0]["id"] == "offline"
    assert "without validation" in capsys.readouterr().out


def test_key_add_accepts_explicit_key_without_echoing_it(isolated_home, capsys) -> None:
    secret = "explicit-provider-key"

    assert (
        cli.main(
            [
                "key",
                "add",
                "personal",
                "--provider",
                "openrouter",
                "--key",
                secret,
                "--no-validate",
            ]
        )
        == 0
    )

    assert list_keys("openrouter") == [
        {"id": "personal", "provider": "openrouter", "label": "personal"}
    ]
    assert secret not in capsys.readouterr().out


def test_key_add_reads_key_path_without_echoing_it(isolated_home, capsys) -> None:
    secret = "file-backed-provider-key"
    key_path = isolated_home / "provider.key"
    key_path.write_text(f"{secret}\n", encoding="utf-8")

    assert (
        cli.main(
            [
                "key",
                "add",
                "agent-key",
                "--provider",
                "google",
                "--key-path",
                str(key_path),
                "--no-validate",
            ]
        )
        == 0
    )

    assert list_keys("google") == [{"id": "agent-key", "provider": "google", "label": "agent-key"}]
    assert secret not in capsys.readouterr().out


def test_select_configures_multiple_provider_routes(
    isolated_home, managed_models, monkeypatch, capsys
) -> None:
    configured: list[tuple[list[dict], int]] = []
    monkeypatch.setattr(cli, "refresh_select_catalogs", lambda: managed_models)
    monkeypatch.setattr(
        cli,
        "_configure",
        lambda models, port: configured.append((models, port)),
    )

    args = Namespace(
        routes=[managed_model(managed_models[0]), managed_model(managed_models[2])],
        port=9876,
        accounts=None,
    )
    assert cli.command_select(args) == 0

    assert configured == [([managed_models[0], managed_models[2]], 9876)]
    output = capsys.readouterr().out
    assert (
        "  - Sonnet 4.6 (1M context) — Claude · Max 20x (a@example.com) via claude-auth-manager"
    ) in output
    assert (
        "  - Gemini 3.8 Flash — Google · Gemini API (google-a) via claude-auth-manager"
    ) in output
    assert "cam/" not in output
    assert "claude-sonnet-4-6" not in output


def test_select_account_filter_disambiguates_and_preserves_other_credentials(
    isolated_home, managed_models, monkeypatch, capsys
) -> None:
    first = managed_models[0]
    second = dict(first, credential="account-b", credential_label="b@example.com")
    google = managed_models[2]
    unavailable = managed_models[1]
    configured: list[list[dict]] = []
    monkeypatch.setattr(cli, "refresh_select_catalogs", lambda: [first, second, google])
    monkeypatch.setattr(cli, "favorite_models", lambda: [second, google, unavailable])
    monkeypatch.setattr(
        cli,
        "favorite_ids",
        lambda: [managed_model(model) for model in (second, google, unavailable)],
    )
    monkeypatch.setattr(
        cli,
        "_configure",
        lambda models, _port: configured.append(models),
    )

    args = Namespace(
        routes=[first["id"]],
        accounts=["a@example.com"],
        port=9876,
    )
    assert cli.command_select(args) == 0

    assert configured == [[second, google, first]]
    output = capsys.readouterr()
    assert "Saved 3 /model favorite(s)" in output.out
    assert "1 saved favorite(s) are no longer available" in output.err


def test_interactive_account_scope_keeps_provider_keys_editable(
    isolated_home, managed_models, monkeypatch
) -> None:
    first = managed_models[0]
    second = dict(first, credential="account-b", credential_label="b@example.com")
    openrouter = managed_models[1]
    google = managed_models[2]
    configured: list[list[dict]] = []
    monkeypatch.setattr(
        cli,
        "refresh_select_catalogs",
        lambda: [first, second, openrouter, google],
    )
    monkeypatch.setattr(cli, "favorite_models", lambda: [second, openrouter, google])
    monkeypatch.setattr(
        cli,
        "favorite_ids",
        lambda: [managed_model(model) for model in (second, openrouter, google)],
    )

    def choose(models, _initial, *, initial_account=None, fallbacks=None, other_favorites=None):
        credentials = {model["credential"] for model in models}
        assert credentials == {"account-a", "router-a", "google-a"}
        assert initial_account == "account-a"
        assert any(model["credential"] == "account-b" for model in other_favorites)
        return [managed_model(first), managed_model(openrouter)]

    monkeypatch.setattr(cli, "choose_models", choose)
    monkeypatch.setattr(cli, "_configure", lambda models, _port: configured.append(models))

    args = Namespace(routes=[], accounts=["a@example.com"], port=9876)
    assert cli.command_select(args) == 0

    assert configured == [[second, first, openrouter]]


def test_account_scope_includes_all_provider_keys(managed_models) -> None:
    first = managed_models[0]
    second = dict(first, credential="account-b", credential_label="b@example.com")

    scoped, accounts = cli._account_scope(
        [first, second, managed_models[1], managed_models[2]], ["a@example.com"]
    )

    assert accounts == {"account-a"}
    assert {model["credential"] for model in scoped} == {
        "account-a",
        "router-a",
        "google-a",
    }


def test_select_rejects_route_removed_by_current_catalog(
    isolated_home, managed_models, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "refresh_select_catalogs", lambda: [managed_models[0]])
    monkeypatch.setattr(
        cli,
        "_configure",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not configure")),
    )
    args = Namespace(
        routes=[managed_model(managed_models[1])],
        accounts=None,
        port=9876,
    )

    with pytest.raises(ValueError, match="not found in the current indexes"):
        cli.command_select(args)


def test_select_account_filter_rejects_unknown_account(managed_models) -> None:
    with pytest.raises(ValueError, match="cam list --account"):
        cli._account_scope(managed_models, ["missing@example.com"])


def test_removing_in_use_key_fails_closed(isolated_home, managed_models) -> None:
    add_key("google", "google-a", "private-google-key")
    save_preferences([managed_models[2]], managed_model(managed_models[2]))
    args = Namespace(key_command="remove", name="google-a")

    with pytest.raises(RuntimeError, match="used by a favorite"):
        cli.command_key(args)

    assert list_keys("google")


def test_check_uses_exact_credential_scoped_route(managed_models, monkeypatch, capsys) -> None:
    checked: list[dict] = []
    passed = ToolProbeResult(True, True, True, 0, None, "ok", "")
    monkeypatch.setattr(cli, "load_all_catalogs", lambda: managed_models)
    monkeypatch.setattr(cli, "probe_model", lambda model: checked.append(model) or passed)

    route = managed_model(managed_models[2])
    assert cli.command_check(route, assume_yes=True) == 0
    assert checked == [managed_models[2]]
    assert route in capsys.readouterr().out


def test_check_without_route_is_the_health_check(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        cli,
        "command_doctor",
        lambda as_json: calls.append(as_json) or 0,
    )

    assert cli.main(["check", "--json"]) == 0
    assert calls == [True]


def test_doctor_reports_metadata_without_secrets(
    isolated_home, managed_models, monkeypatch, capsys
) -> None:
    save_preferences([managed_models[2]], managed_model(managed_models[2]), port=9427)
    add_key("google", "google-a", "private-google-key")
    monkeypatch.setattr(cli, "healthcheck", lambda _port: True)
    monkeypatch.setattr(cli, "has_native_login", lambda: True)

    assert cli.command_doctor(as_json=True) == 0
    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["routes"][0]["ready"] is True
    assert document["routes"][0]["credential"] == "google-a"
    assert "private-google-key" not in output


def test_main_redacts_runtime_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "command_routes",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("safe diagnostic")),
    )
    assert cli.main(["list", "--route"]) == 1
    assert capsys.readouterr().err == "error: safe diagnostic\n"


def test_favorite_models_remain_route_metadata(isolated_home, managed_models) -> None:
    save_preferences(managed_models, managed_model(managed_models[0]))
    assert [managed_model(model) for model in favorite_models()] == [
        managed_model(model) for model in managed_models
    ]
