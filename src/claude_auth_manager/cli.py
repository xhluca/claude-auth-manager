"""Command-line interface for Claude Auth Manager."""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__, google, openrouter
from .agents import run_agent_hook
from .catalogs import (
    account_models,
    exact_routes,
    load_all_catalogs,
    refresh_all_catalogs,
    refresh_key_catalog,
    refresh_select_catalogs,
    search_all,
)
from .check import probe_model
from .fallback import fallback_order, selected_links, validate_links
from .fallback import state_path as fallback_state_path
from .launcher import has_native_login
from .models import (
    claude_model,
    claude_subscription_label,
    compact_model_name,
    managed_model,
    picker_description,
    picker_source,
    supports_tools,
    tool_capability_badge,
)
from .picker import choose_models
from .proxy import DEFAULT_HOST, DEFAULT_PORT, run_router
from .registry import (
    SUPPORTED_KEY_PROVIDERS,
    add_account_token,
    add_current_account,
    add_key,
    key_entry,
    list_accounts,
    list_keys,
    login_account,
    migrate_email_account_ids,
    normalize_id,
    read_account_token,
    read_key,
    remove_account,
    remove_key,
)
from .service import healthcheck, start_service, stop_service
from .settings import (
    assert_private_files,
    configure_claude,
    favorite_ids,
    favorite_models,
    load_preferences,
    reset_integration,
    save_classifier_accounts,
    save_fallbacks,
    save_preferences,
    set_check_confirmation,
)
from .storage import read_json_object
from .uninstall import remove_installed_package
from .update import update_installed_package


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="cam",
        description="Switch Claude subscriptions and provider keys from Claude Code /model.",
    )
    root.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="show the installed version and exit",
    )
    commands = root.add_subparsers(dest="command", metavar="COMMAND")

    listing = commands.add_parser(
        "list",
        help="list credentials, models, routes, or configuration",
        description=(
            "Show non-secret credential metadata, statically filtered models, selected "
            "routes, or manager settings."
        ),
    )
    listing.add_argument(
        "queries",
        nargs="*",
        metavar="QUERY",
        help="with --model, filter model metadata using terms or glob patterns",
    )
    listing_views = listing.add_mutually_exclusive_group()
    listing_views.add_argument(
        "--classifier",
        action="store_true",
        help="show native Sonnet/classifier account order and last routing status",
    )
    listing_views.add_argument(
        "--fallback",
        nargs="?",
        const="",
        metavar="ROUTE",
        help="show ranked fallbacks and effective attempt order for all routes or one ROUTE",
    )
    listing_views.add_argument(
        "--model", dest="models_only", action="store_true", help="show available model routes"
    )
    listing_views.add_argument(
        "--route", dest="routes_only", action="store_true", help="show configured model routes"
    )
    listing_views.add_argument(
        "--config", dest="config_only", action="store_true", help="show manager configuration"
    )
    listing_sources = listing.add_mutually_exclusive_group()
    listing_sources.add_argument(
        "--account",
        nargs="?",
        const="",
        metavar="ACCOUNT",
        help=(
            "without --model, show accounts; with --model, show all subscription models "
            "or only ACCOUNT when supplied"
        ),
    )
    listing_sources.add_argument(
        "--key",
        nargs="?",
        const="",
        metavar="KEY",
        help=(
            "without --model, show keys; with --model, show all API-key models or only "
            "KEY when supplied"
        ),
    )
    listing.add_argument(
        "--provider",
        action="append",
        choices=("anthropic", *sorted(SUPPORTED_KEY_PROVIDERS)),
        help="with --model, keep this provider; repeatable",
    )
    listing.add_argument(
        "--tools", action="store_true", help="with --model, keep models advertising tools"
    )
    listing.add_argument(
        "--offline", action="store_true", help="with --model, use saved catalogs only"
    )
    listing.add_argument(
        "--check-confirmation",
        choices=("ask", "never"),
        help="with --config, change confirmation for billable live checks",
    )
    listing.add_argument("--json", action="store_true", help="emit the selected view as JSON")

    account = commands.add_parser(
        "account",
        help="manage Claude subscription logins",
        description="Add, renew, or remove Claude subscription accounts.",
    )
    account.set_defaults(_help_parser=account, _required_action="account_command")
    account_commands = account.add_subparsers(dest="account_command", metavar="ACTION")
    account_add = account_commands.add_parser(
        "add",
        help="add a Claude login; launches official auth by default",
        description=(
            "Add or renew a Claude subscription using hosted login, native auth, or a setup token."
        ),
    )
    account_add_modes = account_add.add_mutually_exclusive_group()
    account_add_modes.add_argument(
        "--current", action="store_true", help="register the currently active Claude login"
    )
    account_add_modes.add_argument(
        "--token", action="store_true", help="prompt securely for a Claude setup token"
    )
    account_add_modes.add_argument(
        "--token-stdin", action="store_true", help="read a Claude setup token from stdin"
    )
    account_add.add_argument(
        "--name", help="optional nickname; login modes default to email, tokens to a stable ID"
    )
    remove = account_commands.add_parser(
        "remove",
        help="remove one subscription",
        description="Remove one saved Claude account that is not used by a selected route.",
    )
    remove.add_argument("name", help="account ID or nickname to remove")

    key = commands.add_parser(
        "key",
        help="manage provider API keys",
        description="Add, replace, or remove OpenRouter, Google, and Anthropic API keys.",
    )
    key.set_defaults(_help_parser=key, _required_action="key_command")
    key_commands = key.add_subparsers(dest="key_command", metavar="ACTION")
    key_add = key_commands.add_parser(
        "add",
        help="add or replace a provider key",
        description="Store a named provider key and index its models after validation.",
    )
    key_add.add_argument("name", help="required unique nickname used in credential-scoped routes")
    key_add.add_argument(
        "--provider",
        "-p",
        required=True,
        choices=sorted(SUPPORTED_KEY_PROVIDERS),
        help="provider that issued the key",
    )
    key_add.add_argument("--label", help="optional display label; defaults to the key nickname")
    key_secret = key_add.add_mutually_exclusive_group()
    key_secret.add_argument(
        "--key",
        metavar="KEY",
        help="API key value (may be visible in shell history and process listings)",
    )
    key_secret.add_argument(
        "--key-path", type=Path, metavar="PATH", help="read the API key from a file"
    )
    key_secret.add_argument(
        "--key-stdin", action="store_true", help="read the API key from standard input"
    )
    key_add.add_argument(
        "--no-validate",
        action="store_true",
        help="store without validation or indexing; run cam index later",
    )
    key_remove = key_commands.add_parser(
        "remove",
        help="remove one provider key",
        description="Remove one saved provider key that is not used by a selected route.",
    )
    key_remove.add_argument("name", help="key nickname to remove")

    index = commands.add_parser(
        "index",
        help="refresh model catalogs",
        description="Refresh credential-scoped model catalogs for provider keys.",
    )
    index.add_argument("--key", help="refresh only this named provider key")
    index.add_argument("--json", action="store_true", help="emit indexed models as JSON")

    search = commands.add_parser(
        "search",
        help="refresh and search every model catalog",
        description=(
            "Refresh catalogs and search model IDs, names, providers, and credential labels."
        ),
    )
    search.add_argument(
        "queries", nargs="+", help="case-insensitive search terms or glob patterns; any may match"
    )
    search.add_argument("--key", help="search only this named provider key")
    search.add_argument(
        "--tools", action="store_true", help="show only models advertising tool support"
    )
    search.add_argument(
        "--offline", action="store_true", help="search saved catalogs without network refresh"
    )
    search.add_argument("--json", action="store_true", help="emit matching models as JSON")

    select = commands.add_parser(
        "select",
        help="replace /model favorites across providers",
        description=(
            "Refresh each OpenRouter key's guardrail-filtered catalog, then choose /model "
            "favorites interactively or from exact route arguments."
        ),
    )
    select.add_argument(
        "routes",
        nargs="*",
        metavar="ROUTE",
        help="route spec or unique model ID; omit to open the interactive picker",
    )
    select.add_argument(
        "--account",
        dest="accounts",
        action="append",
        metavar="ACCOUNT",
        help=(
            "scope Claude models to this account and initially show it while keeping "
            "provider keys available in the source menu; repeatable"
        ),
    )
    select.add_argument(
        "--port", type=int, help="set and use the local router port (default: configured or 9427)"
    )
    select.add_argument(
        "--fallback",
        nargs="+",
        metavar="TARGET",
        help="replace ROUTE's ranked fallback list, in priority order, without changing favorites",
    )
    classifier_modes = select.add_mutually_exclusive_group()
    classifier_modes.add_argument(
        "--classifier",
        nargs="+",
        metavar="ACCOUNT",
        help="set native Sonnet/classifier account order; preserves the exact requested model",
    )
    classifier_modes.add_argument(
        "--clear-classifier",
        action="store_true",
        help="restore native Sonnet/classifier requests to their original session credentials",
    )
    select.add_argument(
        "--clear-fallback",
        action="append",
        metavar="FROM",
        help="remove this selected route's fallback link; repeatable",
    )

    check = commands.add_parser(
        "check",
        help="check manager health, all credentials, or one live model route",
        description=(
            "Check local health, inspect saved credentials with non-billable provider metadata, "
            "or send a potentially billable tool probe through one route."
        ),
    )
    check.add_argument(
        "route", nargs="?", help="configured route to probe; omit for non-billable local health"
    )
    check_sources = check.add_mutually_exclusive_group()
    check_sources.add_argument(
        "--all",
        action="store_true",
        help="check every saved account/key using non-billable provider metadata",
    )
    check_sources.add_argument(
        "--account",
        nargs="?",
        const="",
        metavar="ACCOUNT",
        help="check all saved subscriptions, or one account ID/label (non-billable)",
    )
    check_sources.add_argument(
        "--key",
        nargs="?",
        const="",
        metavar="KEY",
        help="check all saved API keys, or one key ID/label (non-billable)",
    )
    check.add_argument(
        "-y", "--yes", action="store_true", help="send the live route probe without confirmation"
    )
    check.add_argument(
        "--json",
        action="store_true",
        help="emit health, credential status, or probe results as JSON",
    )

    serve = commands.add_parser(
        "serve",
        help="run the local credential router in the foreground",
        description="Run the authenticated credential router on a loopback address.",
    )
    serve.add_argument(
        "--host", default=DEFAULT_HOST, help="loopback bind address (default: 127.0.0.1)"
    )
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help="listen port (default: 9427)")

    commands.add_parser(
        "reset",
        help="restore Claude settings and delete manager data",
        description="Stop the router, restore pre-CAM Claude settings, and delete all CAM data.",
    )
    commands.add_parser(
        "uninstall",
        help="reset integration and uninstall this tool",
        description="Run cam reset, then remove the CAM package when its installer is recognized.",
    )
    commands.add_parser(
        "update",
        help="update the installed manager",
        description="Update CAM in place with the package manager that owns the installation.",
    )
    return root


def _masked_input(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return getpass.getpass(prompt)
    try:
        import termios
    except ImportError:
        return getpass.getpass(prompt)
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    masked = original.copy()
    masked[6] = original[6].copy()
    masked[3] &= ~(termios.ECHO | termios.ICANON)
    masked[6][termios.VMIN] = 1
    masked[6][termios.VTIME] = 0
    characters: list[str] = []
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, masked)
        while True:
            character = sys.stdin.read(1)
            if character in {"\n", "\r"}:
                sys.stderr.write("\n")
                return "".join(characters)
            if character in {"\b", "\x7f"}:
                if characters:
                    characters.pop()
                    sys.stderr.write("\b \b")
                    sys.stderr.flush()
                continue
            if character == "\x04" and not characters:
                raise EOFError
            if character.isprintable():
                characters.append(character)
                sys.stderr.write("*")
                sys.stderr.flush()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)


def _read_secret(label: str, *, stdin: bool) -> str:
    value = sys.stdin.readline().strip() if stdin else _masked_input(f"{label}: ").strip()
    if not value:
        raise ValueError(f"{label} cannot be empty")
    return value


def _display_models(models: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        records = []
        for model in models:
            record = dict(model)
            record["route"] = managed_model(model)
            records.append(record)
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return
    _print_table(
        ("ROUTE", "MODEL", "NAME", "TOOLS"),
        [
            (
                managed_model(model),
                model["id"],
                model.get("name", ""),
                tool_capability_badge(model),
            )
            for model in models
        ],
    )


def _print_table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
    """Print stable columns without relying on terminal-dependent tab stops."""
    values = [tuple(str(value) for value in row) for row in rows]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in values))
        for index in range(len(headers) - 1)
    ]

    def render(row: tuple[str, ...]) -> str:
        leading = [row[index].ljust(widths[index]) for index in range(len(widths))]
        return "  ".join((*leading, row[-1])).rstrip()

    print(render(headers))
    for row in values:
        print(render(row))


def _validate_provider_key(provider: str, key: str) -> None:
    if provider == "openrouter":
        openrouter.validate_key_shape(key)
        openrouter.validate_key(key)
    elif provider == "google":
        google.validate_key(key)
    elif provider == "anthropic-api":
        from .anthropic import validate_anthropic_key_shape

        validate_anthropic_key_shape(key)


def command_key_add(
    provider: str,
    name: str,
    *,
    label: str | None,
    key_stdin: bool,
    no_validate: bool,
    key_value: str | None = None,
    key_path: Path | None = None,
) -> int:
    if key_path is not None:
        try:
            secret = key_path.expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"could not read API key file {key_path}: {exc}") from exc
        if not secret:
            raise ValueError(f"{provider} API key file is empty")
    elif key_value is None:
        secret = _read_secret(f"{provider} API key", stdin=key_stdin)
    else:
        secret = key_value.strip()
        if not secret:
            raise ValueError(f"{provider} API key cannot be empty")
    if not no_validate:
        _validate_provider_key(provider, secret)
    entry = add_key(provider, name, secret, label=label)
    models = [] if no_validate else refresh_key_catalog(str(entry["id"]))
    assert_private_files()
    if no_validate:
        print(
            f"Saved {provider} key {entry['id']} without validation; "
            f"run `cam index --key {entry['id']}`."
        )
    else:
        print(f"Saved {provider} key {entry['id']} and indexed {len(models)} models.")
    return 0


def _route_uses(kind: str, credential: str) -> bool:
    return any(
        model.get("provider") == kind and model.get("credential") == credential
        for model in favorite_models()
    )


def _replace_account_route(route: Any, mapping: dict[str, str]) -> Any:
    if not isinstance(route, str):
        return route
    for old, new in mapping.items():
        prefix = f"cam/anthropic/{old}/"
        if route.startswith(prefix):
            return f"cam/anthropic/{new}/{route[len(prefix) :]}"
    return route


def _migrate_configured_account_ids() -> None:
    """Upgrade old email slugs and regenerate CAM-owned route configuration once."""
    mapping = migrate_email_account_ids()
    if not mapping:
        return
    preferences = load_preferences()
    favorites = preferences.get("favorites", [])
    if not favorites or not all(isinstance(model, dict) for model in favorites):
        return
    models = [dict(model) for model in favorites]
    changed = False
    for model in models:
        if model.get("provider") == "anthropic" and model.get("credential") in mapping:
            model["credential"] = mapping[str(model["credential"])]
            changed = True
    if not changed:
        return
    port = preferences.get("router_port", DEFAULT_PORT)
    if not isinstance(port, int):
        raise RuntimeError("invalid router port")
    default_model = _replace_account_route(preferences.get("default_model"), mapping)
    if not isinstance(default_model, str):
        default_model = managed_model(models[0])
    # Seed the new default before configure_claude reads preferences, so a
    # non-first selected route remains the default after its account ID changes.
    save_preferences(models, default_model, port=port)
    configure_claude(models, native_login=has_native_login(), port=port)
    start_service(port)


def command_list(
    *,
    account: str | None = None,
    key: str | None = None,
    models_only: bool = False,
    routes_only: bool = False,
    config_only: bool = False,
    fallback: str | None = None,
    classifier: bool = False,
    queries: list[str] | None = None,
    providers: list[str] | None = None,
    tools_only: bool = False,
    offline: bool = False,
    check_confirmation: str | None = None,
    as_json: bool,
) -> int:
    """List non-secret credential, model, route, or configuration metadata."""
    queries = queries or []
    providers = providers or []
    if classifier:
        from .classifier import accounts as classifier_accounts

        if (
            account is not None
            or key is not None
            or queries
            or providers
            or tools_only
            or offline
            or check_confirmation
        ):
            raise ValueError("--classifier accepts only --json")
        state = read_json_object(fallback_state_path(), missing_ok=True)
        result = {
            "accounts": classifier_accounts(load_preferences()),
            "scope": "native Sonnet requests, including permission classifiers",
            "active": {
                key: value
                for key, value in state.get("active", {}).items()
                if key.startswith("classifier/")
            },
            "failures": {
                key: value
                for key, value in state.get("failures", {}).items()
                if key.startswith("classifier/")
            },
        }
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print("Native Sonnet/classifier account order:")
            for rank, value in enumerate(result["accounts"], 1):
                print(f"  {rank}. {value}")
            if not result["accounts"]:
                print("  Disabled — original session credentials")
            print("Last routing status: " + json.dumps(result["active"]))
        return 0
    if fallback is not None:
        if (
            account is not None
            or key is not None
            or queries
            or providers
            or tools_only
            or offline
            or check_confirmation
        ):
            raise ValueError("--fallback accepts only an optional ROUTE and --json")
        models = favorite_models()
        links = selected_links(load_preferences())
        routes = [
            managed_model(model)
            for model in (exact_routes(models, [fallback]) if fallback else models)
        ]
        rows = [
            {
                "route": route,
                "fallbacks": links.get(route, []),
                "attempt_order": fallback_order(route, links),
            }
            for route in routes
        ]
        if as_json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(row["route"])
                for rank, target in enumerate(row["fallbacks"], 1):
                    print(f"  {rank}. {target}")
                if not row["fallbacks"]:
                    print("  No fallbacks")
                print("  Attempt order: " + " → ".join(row["attempt_order"]))
        return 0
    if check_confirmation is not None and not config_only:
        raise ValueError("--check-confirmation requires --config")
    if not models_only and (queries or providers or tools_only or offline):
        raise ValueError("QUERY, --provider, --tools, and --offline require --model")
    if not models_only and account not in {None, ""}:
        raise ValueError("an ACCOUNT value requires --model")
    if not models_only and key not in {None, ""}:
        raise ValueError("a KEY value requires --model")
    if (routes_only or config_only) and (account is not None or key is not None):
        raise ValueError("--account and --key cannot be combined with --route or --config")
    _migrate_configured_account_ids()
    if models_only:
        models = load_all_catalogs() if offline else refresh_select_catalogs()
        if account is not None:
            models = [model for model in models if model.get("provider") == "anthropic"]
            if account:
                models, _selected = _account_scope(models, [account])
        elif key is not None:
            models = _key_scope(models, key)
        if providers:
            allowed = set(providers)
            models = [model for model in models if model.get("provider") in allowed]
        if queries:
            models = search_all(models, queries)
        if tools_only:
            models = [model for model in models if supports_tools(model)]
        _display_models(models, as_json=as_json)
        return 0 if models else 1
    if routes_only:
        return command_routes(as_json=as_json)
    if config_only:
        if check_confirmation is not None:
            set_check_confirmation(check_confirmation == "ask")
        preferences = load_preferences()
        summary = {
            "default_model": preferences.get("default_model"),
            "router_port": preferences.get("router_port", DEFAULT_PORT),
            "check_confirmation": (
                "ask" if preferences.get("confirm_billable_checks", True) else "never"
            ),
            "routes": favorite_ids(),
            "fallbacks": selected_links(preferences),
            "fallback_state": read_json_object(fallback_state_path(), missing_ok=True),
        }
        if as_json:
            print(json.dumps(summary, indent=2))
        else:
            _print_table(
                ("SETTING", "VALUE"),
                [
                    ("default_model", summary["default_model"] or "-"),
                    ("router_port", summary["router_port"]),
                    ("check_confirmation", summary["check_confirmation"]),
                    ("routes", len(summary["routes"])),
                    ("fallbacks", json.dumps(summary["fallbacks"], ensure_ascii=False)),
                    ("fallback_state", json.dumps(summary["fallback_state"], ensure_ascii=False)),
                ],
            )
        return 0
    accounts = list_accounts() if key is None else []
    keys = list_keys() if account is None else []
    rows = [dict({"type": "account"}, **entry) for entry in accounts]
    rows.extend(dict({"type": "key"}, **entry) for entry in keys)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    display_rows = []
    for entry in rows:
        if entry["type"] == "account":
            source = " · ".join(
                str(value)
                for value in (
                    claude_subscription_label(
                        entry.get("subscription"), entry.get("rate_limit_tier")
                    ),
                    entry.get("source"),
                )
                if value
            )
        else:
            source = str(entry.get("provider") or "-")
        display_rows.append((entry["type"], entry["id"], entry.get("label") or "-", source or "-"))
    _print_table(("TYPE", "ID", "LABEL", "SOURCE"), display_rows)
    return 0


def _key_scope(models: list[dict[str, Any]], requested: str) -> list[dict[str, Any]]:
    keyed = [model for model in models if model.get("provider") != "anthropic"]
    if not requested:
        return keyed
    query = requested.strip().casefold()
    matches = [
        str(entry["id"])
        for entry in list_keys()
        if query
        in {
            str(entry["id"]).casefold(),
            str(entry.get("label") or "").strip().casefold(),
        }
    ]
    if not matches:
        raise ValueError(f"provider key not found: {requested}; run `cam list --key`")
    if len(matches) > 1:
        raise ValueError(f"provider key label is ambiguous: {requested}; use its key ID")
    return [model for model in keyed if model.get("credential") == matches[0]]


def _bootstrap_account(account_id: str) -> bool:
    """Configure a usable default route when the first account is added."""
    if favorite_models():
        return False
    selected = [
        model
        for model in account_models()
        if model.get("credential") == account_id and model.get("id") == "claude-sonnet-5"
    ]
    if not selected:
        raise RuntimeError(f"could not create a default route for Claude account {account_id}")
    preferences = load_preferences()
    port = preferences.get("router_port", DEFAULT_PORT)
    if not isinstance(port, int):
        raise RuntimeError("invalid router port")
    _configure(selected, port)
    print(f"Initialized default route {managed_model(selected[0])}.")
    return True


def command_account(args: argparse.Namespace) -> int:
    if args.account_command == "add":
        if args.current:
            entry = add_current_account(args.name)
            print(f"Registered current Claude subscription {entry['id']} ({entry.get('email')}).")
        elif args.token or args.token_stdin:
            token = _read_secret("Claude setup token", stdin=args.token_stdin)
            entry = add_account_token(args.name, token)
            print(f"Saved Claude subscription token {entry['id']}.")
        else:
            entry = login_account(args.name)
            action = "Updated" if entry.get("updated") else "Saved"
            print(f"{action} Claude subscription {entry['id']} ({entry.get('email')}).")
        _bootstrap_account(str(entry["id"]))
    elif args.account_command == "remove":
        account_id = normalize_id(args.name)
        from .classifier import accounts as classifier_accounts

        if account_id in classifier_accounts(load_preferences()):
            raise RuntimeError(
                "account is in the classifier chain; edit or clear it before removal"
            )
        if _route_uses("anthropic", account_id):
            raise RuntimeError("account is used by a favorite; run cam select before removing it")
        remove_account(account_id)
        print(f"Removed Claude account {account_id}.")
    assert_private_files()
    return 0


def command_key(args: argparse.Namespace) -> int:
    if args.key_command == "add":
        return command_key_add(
            args.provider,
            args.name,
            label=args.label,
            key_stdin=args.key_stdin,
            no_validate=args.no_validate,
            key_value=args.key,
            key_path=args.key_path,
        )
    key_id = normalize_id(args.name)
    entry = key_entry(key_id)
    if _route_uses(str(entry["provider"]), key_id):
        raise RuntimeError("key is used by a favorite; run cam select before removing it")
    remove_key(key_id)
    print(f"Removed provider key {key_id}.")
    return 0


def _configure(selected: list[dict[str, Any]], port: int) -> None:
    configure_claude(selected, native_login=has_native_login(), port=port)
    start_service(port)
    assert_private_files()


def command_index(*, key_id: str | None, as_json: bool) -> int:
    models = refresh_key_catalog(key_id) if key_id else refresh_all_catalogs()
    _display_models(models, as_json=as_json)
    if not as_json:
        print(f"Indexed {len(models)} credential-scoped model routes.", file=sys.stderr)
    return 0


def command_search(args: argparse.Namespace) -> int:
    if args.key:
        models = load_all_catalogs() if args.offline else refresh_key_catalog(args.key)
        models = [model for model in models if model.get("credential") == args.key]
    else:
        models = load_all_catalogs() if args.offline else refresh_all_catalogs()
    found = search_all(models, args.queries)
    if args.tools:
        found = [model for model in found if supports_tools(model)]
    _display_models(found, as_json=args.json)
    return 0 if found else 1


def _account_scope(
    models: list[dict[str, Any]], requested: list[str]
) -> tuple[list[dict[str, Any]], set[str]]:
    accounts: dict[str, set[str]] = {}
    for model in models:
        credential = model.get("credential")
        if model.get("provider") != "anthropic" or not isinstance(credential, str):
            continue
        names = accounts.setdefault(credential, {credential.casefold()})
        label = model.get("credential_label")
        if isinstance(label, str) and label.strip():
            names.add(label.strip().casefold())
    selected: set[str] = set()
    for value in requested:
        query = value.strip().casefold()
        matches = [credential for credential, names in accounts.items() if query in names]
        if not matches:
            raise ValueError(f"Claude account not found: {value}; run `cam list --account`")
        if len(matches) > 1:
            raise ValueError(f"Claude account label is ambiguous: {value}; use its account ID")
        selected.add(matches[0])
    return (
        [
            model
            for model in models
            if model.get("provider") != "anthropic" or model.get("credential") in selected
        ],
        selected,
    )


def _edit_fallbacks(
    args: argparse.Namespace, models: list[dict], links: dict[str, list[str]]
) -> None:
    def resolve(value: str) -> str:
        return managed_model(exact_routes(models, [value])[0])

    for source in getattr(args, "clear_fallback", None) or []:
        links.pop(resolve(source), None)
    if getattr(args, "fallback", None):
        if len(args.routes) != 1:
            raise ValueError("use cam select ROUTE --fallback TARGET [TARGET ...]")
        links[resolve(args.routes[0])] = [resolve(target) for target in args.fallback]
    validate_links(links, {managed_model(model) for model in models})


def command_select(args: argparse.Namespace) -> int:
    preferences = load_preferences()
    if getattr(args, "classifier", None) or getattr(args, "clear_classifier", False):
        from .classifier import accounts as classifier_accounts

        if args.routes or args.accounts or args.port or args.fallback or args.clear_fallback:
            raise ValueError(
                "--classifier/--clear-classifier cannot be combined with model selection flags"
            )
        saved = list_accounts()
        chosen = []
        for value in args.classifier or []:
            matches = [
                entry["id"]
                for entry in saved
                if value in {entry["id"], entry.get("label"), entry.get("email")}
            ]
            if len(matches) != 1:
                raise ValueError(f"unknown or ambiguous Claude account: {value}")
            chosen.append(matches[0])
        classifier_accounts({"classifier_accounts": chosen})
        save_classifier_accounts(chosen)
        print(
            "✓ Saved native Sonnet/classifier account order. Applies on the router's next request."
        )
        return 0
    links = selected_links(preferences)
    edits = getattr(args, "fallback", None) or getattr(args, "clear_fallback", None)
    if edits:
        if getattr(args, "accounts", None) or args.port:
            raise ValueError("fallback-only edits cannot use --account or --port")
        _edit_fallbacks(args, favorite_models(), links)
        save_fallbacks(links)
        print(f"✓ Saved {len(links)} fallback link(s). Router applies changes on its next request.")
        return 0
    openrouter_keys = list_keys("openrouter")
    if openrouter_keys:
        print(
            f"Refreshing {len(openrouter_keys)} OpenRouter key catalog(s) and guardrails…",
            file=sys.stderr,
        )
    available_models = refresh_select_catalogs()
    available_routes = {
        route for model in available_models for route in (managed_model(model), claude_model(model))
    }
    unavailable_favorites = [route for route in favorite_ids() if route not in available_routes]
    if unavailable_favorites:
        print(
            f"Notice: {len(unavailable_favorites)} saved favorite(s) are no longer "
            "available under current provider guardrails/catalogs and will be removed if saved.",
            file=sys.stderr,
        )
    models = available_models
    account_filters = list(getattr(args, "accounts", None) or [])
    scoped_accounts: set[str] = set()
    if account_filters:
        models, scoped_accounts = _account_scope(models, account_filters)
    requested = args.routes
    if requested:
        selected = exact_routes(models, requested)
    else:
        picker_models: list[dict[str, Any]] = []
        for model in models:
            row = dict(model)
            row["selection_id"] = managed_model(model)
            row["description"] = picker_description(model)
            row["display_source"] = picker_source(model)
            picker_models.append(row)
        initial_account = next(iter(scoped_accounts)) if len(scoped_accounts) == 1 else None
        initial_favorites = set(favorite_ids())
        other_favorites = [
            dict(
                model,
                selection_id=managed_model(model),
                display_source=picker_source(model),
                description=picker_description(model),
            )
            for model in available_models
            if managed_model(model) in initial_favorites
        ]
        chosen = choose_models(
            picker_models,
            favorite_ids(),
            initial_account=initial_account,
            fallbacks=links,
            other_favorites=other_favorites,
        )
        if chosen is None:
            raise RuntimeError("selection cancelled")
        selected = exact_routes(models, chosen)
    if scoped_accounts:
        current_by_route = {managed_model(model): model for model in available_models}
        preserved: list[dict[str, Any]] = []
        for favorite in favorite_models():
            if (
                favorite.get("provider") == "anthropic"
                and favorite.get("credential") in scoped_accounts
            ):
                continue
            if not requested and favorite.get("provider") != "anthropic":
                # Provider-key routes were visible and editable in the scoped
                # interactive picker, so its selection is authoritative.
                continue
            current = current_by_route.get(managed_model(favorite))
            if current is not None:
                preserved.append(current)
        combined: dict[str, dict[str, Any]] = {}
        for model in (*preserved, *selected):
            combined[managed_model(model)] = model
        selected = list(combined.values())
    preferences = load_preferences()
    port = args.port or preferences.get("router_port", DEFAULT_PORT)
    if not isinstance(port, int):
        raise RuntimeError("invalid router port")
    selected_ids = {managed_model(model) for model in selected}
    links = {
        source: [target for target in targets if target in selected_ids]
        for source, targets in links.items()
        if source in selected_ids and any(target in selected_ids for target in targets)
    }
    _edit_fallbacks(args, selected, links)
    _configure(selected, port)
    save_fallbacks(links)
    print(f"✓ Saved {len(selected)} /model favorite(s):")
    for model in selected:
        print(f"  - {compact_model_name(model)} — {picker_source(model)}")
    print("Existing Claude sessions: run /agents, then reopen /model.")
    return 0


def _confirm_check(model: dict[str, Any], assume_yes: bool) -> bool:
    if assume_yes or load_preferences().get("confirm_billable_checks", True) is False:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError("live checks require confirmation in a terminal; rerun with --yes")
    answer = input(f"Send one live request through {managed_model(model)}? [y/N]: ")
    return answer.strip().casefold() in {"y", "yes"}


def command_check(
    route: str | None,
    *,
    assume_yes: bool,
    as_json: bool = False,
    all_credentials: bool = False,
    account: str | None = None,
    key: str | None = None,
) -> int:
    if all_credentials or account is not None or key is not None:
        if route is not None:
            raise ValueError("a live model ROUTE cannot be combined with credential status filters")
        from .status import check_credentials, usage_summary

        result = check_credentials(account=account, key=key)
        if as_json:
            print(json.dumps(result, indent=2))
        elif not result["credentials"]:
            print("No saved credentials match this check.")
        else:
            providers = {
                "anthropic": "Claude",
                "openrouter": "OpenRouter",
                "google": "Google",
                "anthropic-api": "Anthropic API",
            }
            _print_table(
                ("TYPE", "ID", "PROVIDER", "STATUS", "USAGE / DETAIL"),
                [
                    (
                        r["type"],
                        r["id"],
                        providers.get(r["provider"], r["provider"]),
                        r["status"],
                        usage_summary(r),
                    )
                    for r in result["credentials"]
                ],
            )
            print("Non-billable metadata checks; use cam check ROUTE for an inference/tool test.")
        return 0 if result["passed"] else 1
    if route is None:
        return command_doctor(as_json=as_json)
    model = exact_routes(load_all_catalogs(), [route])[0]
    if not _confirm_check(model, assume_yes):
        print("Cancelled; no request was sent.")
        return 0
    result = probe_model(model)
    if as_json:
        print(
            json.dumps(
                {
                    "route": managed_model(model),
                    "passed": result.passed,
                    "tool_called": result.tool_called,
                    "tool_completed": result.tool_completed,
                    "acknowledged_result": result.acknowledged_result,
                    "returncode": result.returncode,
                    "total_cost_usd": result.total_cost_usd,
                    "diagnostic": result.diagnostic,
                },
                indent=2,
            )
        )
        return 0 if result.passed else 1
    if result.passed:
        print(f"✓ Tool round-trip passed through {managed_model(model)}")
        return 0
    print("✗ Tool round-trip failed", file=sys.stderr)
    if result.diagnostic:
        print(result.diagnostic.splitlines()[0][:500], file=sys.stderr)
    return 1


def command_routes(*, as_json: bool) -> int:
    _display_models(favorite_models(), as_json=as_json)
    return 0


def command_doctor(*, as_json: bool) -> int:
    preferences = load_preferences()
    models = favorite_models()
    port = preferences.get("router_port", DEFAULT_PORT)
    route_credentials: list[dict[str, Any]] = []
    healthy_credentials = True
    for model in models:
        provider = str(model.get("provider"))
        credential = str(model.get("credential"))
        try:
            if provider == "anthropic":
                read_account_token(credential)
            else:
                read_key(credential, provider=provider)
            ok = True
        except (OSError, RuntimeError, ValueError):
            ok = False
            healthy_credentials = False
        route_credentials.append(
            {
                "route": managed_model(model),
                "provider": provider,
                "credential": credential,
                "ready": ok,
            }
        )
    status = {
        "configured": bool(models),
        "router": healthcheck(port) if isinstance(port, int) else False,
        "router_url": f"http://127.0.0.1:{port}",
        "native_login": has_native_login(),
        "accounts": len(list_accounts()),
        "keys": len(list_keys()),
        "routes": route_credentials,
    }
    healthy = bool(status["configured"] and status["router"] and healthy_credentials)
    if as_json:
        print(json.dumps(status, indent=2))
    else:
        print(f"Configuration: {'ready' if status['configured'] else 'missing'}")
        print(f"Router: {'healthy' if status['router'] else 'unavailable'}")
        for route in route_credentials:
            print(f"  {'✓' if route['ready'] else '✗'} {route['route']}")
    return 0 if healthy else 1


def command_reset() -> int:
    stop_service()
    restored = reset_integration()
    print("Restored Claude Code settings." if restored else "Claude settings needed no restore.")
    print("Removed all manager accounts, API keys, catalogs, and state.")
    return 0


def command_uninstall() -> int:
    command_reset()
    if remove_installed_package():
        print("Uninstalled claude-auth-manager.")
    else:
        print("Remove this development install with its package manager.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw == ["_agent-hook"]:
        return run_agent_hook()
    root = parser()
    args = root.parse_args(raw)
    if args.command is None:
        root.print_help()
        return 0
    required_action = getattr(args, "_required_action", None)
    if isinstance(required_action, str) and getattr(args, required_action, None) is None:
        args._help_parser.print_help()
        return 0
    try:
        if args.command == "list":
            return command_list(
                account=args.account,
                key=args.key,
                models_only=args.models_only,
                routes_only=args.routes_only,
                config_only=args.config_only,
                fallback=args.fallback,
                classifier=args.classifier,
                queries=args.queries,
                providers=args.provider,
                tools_only=args.tools,
                offline=args.offline,
                check_confirmation=args.check_confirmation,
                as_json=args.json,
            )
        if args.command == "account":
            return command_account(args)
        if args.command == "key":
            return command_key(args)
        if args.command == "index":
            return command_index(key_id=args.key, as_json=args.json)
        if args.command == "search":
            return command_search(args)
        if args.command == "select":
            return command_select(args)
        if args.command == "check":
            return command_check(
                args.route,
                assume_yes=args.yes,
                as_json=args.json,
                all_credentials=args.all,
                account=args.account,
                key=args.key,
            )
        if args.command == "serve":
            run_router(args.host, args.port)
            return 0
        if args.command == "reset":
            return command_reset()
        if args.command == "uninstall":
            return command_uninstall()
        if args.command == "update":
            preferences = load_preferences()
            update_installed_package(__version__)
            port = preferences.get("router_port", DEFAULT_PORT)
            if favorite_models() and isinstance(port, int):
                start_service(port)
            return 0
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2
