"""Dependency-free interactive multi-model picker."""

from __future__ import annotations

import curses
import os
import sys
from contextlib import suppress
from typing import Any

from .fallback import validate_links
from .models import claude_subscription_label, compact_model_name, top_matches

PAIR_TITLE = 1
PAIR_ACCENT = 2
PAIR_SUCCESS = 3
PAIR_QUERY = 4
PAIR_CURSOR = 5
_COLORS_ENABLED = False


def _init_colors() -> None:
    global _COLORS_ENABLED
    _COLORS_ENABLED = False
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return
    try:
        curses.start_color()
        if not curses.has_colors():
            return
        background = -1
        try:
            curses.use_default_colors()
        except curses.error:
            background = curses.COLOR_BLACK
        curses.init_pair(PAIR_TITLE, curses.COLOR_MAGENTA, background)
        curses.init_pair(PAIR_ACCENT, curses.COLOR_CYAN, background)
        curses.init_pair(PAIR_SUCCESS, curses.COLOR_GREEN, background)
        curses.init_pair(PAIR_QUERY, curses.COLOR_YELLOW, background)
        curses.init_pair(PAIR_CURSOR, curses.COLOR_BLACK, curses.COLOR_CYAN)
    except curses.error:
        return
    _COLORS_ENABLED = True


def _style(pair: int, attributes: int = curses.A_NORMAL) -> int:
    return attributes | (curses.color_pair(pair) if _COLORS_ENABLED else 0)


def _add_segments(
    screen: Any,
    row: int,
    width: int,
    segments: list[tuple[str, int]],
) -> None:
    column = 0
    limit = max(1, width - 1)
    for value, style in segments:
        remaining = limit - column
        if remaining <= 0:
            break
        screen.addnstr(row, column, value, remaining, style)
        column += min(len(value), remaining)


def _set_cursor(visible: bool) -> None:
    with suppress(curses.error):
        curses.curs_set(1 if visible else 0)


def _ordered_toggle(selected: list[str], model_id: str) -> None:
    if model_id in selected:
        selected.remove(model_id)
    else:
        selected.append(model_id)


def _selection_id(model: dict[str, Any]) -> str:
    value = model.get("selection_id", model.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError("picker model has no selection id")
    return value


SourceId = tuple[str, str] | None
SourceChoice = tuple[SourceId, str]


def _source_choices(models: list[dict[str, Any]]) -> list[SourceChoice]:
    choices: list[SourceChoice] = [(None, "All accounts and keys")]
    seen: set[tuple[str, str]] = set()
    for model in models:
        provider = model.get("provider")
        credential = model.get("credential")
        if not isinstance(provider, str) or not isinstance(credential, str):
            continue
        source_id = (provider, credential)
        if source_id in seen:
            continue
        seen.add(source_id)
        label = model.get("credential_label")
        name = str(label or credential)
        subscription = claude_subscription_label(
            model.get("subscription"), model.get("rate_limit_tier")
        )
        source = {
            "anthropic": f"Claude · {subscription}",
            "anthropic-api": "Claude · Anthropic API",
            "openrouter": "OpenRouter · API key",
            "google": "Google · Gemini API",
        }.get(provider, provider)
        choices.append((source_id, f"{source} ({name})"))
    return choices


def _source_results(
    models: list[dict[str, Any]], query: str, source_id: SourceId
) -> list[dict[str, Any]]:
    scoped = (
        models
        if source_id is None
        else [
            model
            for model in models
            if (model.get("provider"), model.get("credential")) == source_id
        ]
    )
    return top_matches(scoped, query)


def _initial_source_index(sources: list[SourceChoice], initial_account: str | None) -> int:
    if initial_account is None:
        return 0
    return next(
        (
            index
            for index, (source_id, _label) in enumerate(sources)
            if source_id == ("anthropic", initial_account)
        ),
        0,
    )


def _draw(
    screen: Any,
    models: list[dict[str, Any]],
    query: str,
    cursor: int,
    selected: list[str],
    search_mode: bool,
    catalog: list[dict[str, Any]] | None = None,
    source_label: str = "All accounts and keys",
    fallbacks: dict[str, str] | None = None,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    title = "Claude Auth Manager — choose /model favorites"
    screen.addnstr(0, 0, title, max(1, width - 1), _style(PAIR_TITLE, curses.A_BOLD))
    prompt = f"Search: {query}"
    _add_segments(
        screen,
        2,
        width,
        [
            ("Search: ", _style(PAIR_ACCENT, curses.A_BOLD)),
            (query, _style(PAIR_QUERY, curses.A_BOLD)),
        ],
    )
    _add_segments(
        screen,
        3,
        width,
        [
            ("Source: ", _style(PAIR_ACCENT, curses.A_BOLD)),
            (source_label, _style(PAIR_QUERY, curses.A_BOLD)),
            ("  [Tab to change]", _style(PAIR_ACCENT, curses.A_DIM)),
        ],
    )
    if search_mode:
        screen.addnstr(
            4,
            0,
            "Type to filter · ↓/Enter browse · Ctrl-S/Shift-S save · Ctrl-C cancel",
            width - 1,
            _style(PAIR_ACCENT, curses.A_DIM),
        )
    else:
        screen.addnstr(
            4,
            0,
            "↑/↓ move · Enter/Space select · Ctrl-F fallback · Esc search · s save · q cancel",
            width - 1,
            _style(PAIR_ACCENT, curses.A_DIM),
        )
    _add_segments(
        screen,
        5,
        width,
        [
            ("Selected: ", _style(PAIR_ACCENT, curses.A_BOLD)),
            (str(len(selected)), _style(PAIR_SUCCESS, curses.A_BOLD)),
        ],
    )
    available_rows = max(1, height - 8)
    start = max(0, cursor - available_rows + 1)
    for row_index, model in enumerate(models[start : start + available_rows], start=7):
        absolute = start + row_index - 7
        model_id = _selection_id(model)
        mark = "●" if model_id in selected else "○"
        label = compact_model_name(model)
        source = model.get("display_source") or model.get("description")
        suffix = f" — {source}" if isinstance(source, str) and source else ""
        target = (fallbacks or {}).get(model_id)
        fallback_model = next(
            (item for item in (catalog or []) if _selection_id(item) == target), None
        )
        if fallback_model:
            credential = fallback_model.get("credential_label") or fallback_model.get("credential")
            suffix = f" → {compact_model_name(fallback_model)} ({credential})" + suffix
        if not search_mode and absolute == cursor:
            screen.addnstr(
                row_index,
                0,
                f"{mark} {label}{suffix}",
                width - 1,
                _style(PAIR_CURSOR, curses.A_BOLD),
            )
        else:
            mark_style = _style(
                PAIR_SUCCESS if model_id in selected else PAIR_ACCENT,
                curses.A_BOLD if model_id in selected else curses.A_DIM,
            )
            _add_segments(
                screen,
                row_index,
                width,
                [
                    (f"{mark} ", mark_style),
                    (label, _style(PAIR_ACCENT, curses.A_BOLD)),
                    (suffix, curses.A_DIM),
                ],
            )
    if search_mode:
        screen.move(2, min(width - 1, len(prompt)))
        _set_cursor(True)
    else:
        _set_cursor(False)
    screen.refresh()


def _matching_sources(sources: list[SourceChoice], query: str) -> list[tuple[int, SourceChoice]]:
    needle = query.strip().casefold()
    return [
        (index, source)
        for index, source in enumerate(sources)
        if not needle
        or needle in source[1].casefold()
        or (source[0] is not None and needle in " ".join(source[0]).casefold())
    ]


def _draw_source_menu(
    screen: Any,
    matches: list[tuple[int, SourceChoice]],
    query: str,
    cursor: int,
    selected_index: int,
) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    screen.addnstr(
        0,
        0,
        "Claude Auth Manager — filter by account or key",
        max(1, width - 1),
        _style(PAIR_TITLE, curses.A_BOLD),
    )
    _add_segments(
        screen,
        2,
        width,
        [
            ("Search sources: ", _style(PAIR_ACCENT, curses.A_BOLD)),
            (query, _style(PAIR_QUERY, curses.A_BOLD)),
        ],
    )
    screen.addnstr(
        3,
        0,
        "Type to filter · ↑/↓ move · Enter apply · Esc return",
        max(1, width - 1),
        _style(PAIR_ACCENT, curses.A_DIM),
    )
    available_rows = max(1, height - 6)
    start = max(0, cursor - available_rows + 1)
    for row, (source_index, (_source_id, label)) in enumerate(
        matches[start : start + available_rows], start=5
    ):
        absolute = start + row - 5
        mark = "●" if source_index == selected_index else "○"
        screen.addnstr(
            row,
            0,
            f"{mark} {label}",
            max(1, width - 1),
            _style(PAIR_CURSOR if absolute == cursor else PAIR_ACCENT, curses.A_BOLD),
        )
    if not matches:
        screen.addnstr(5, 0, "No matching accounts or keys.", width - 1, curses.A_DIM)
    screen.move(2, min(width - 1, len("Search sources: ") + len(query)))
    _set_cursor(True)
    screen.refresh()


def _source_menu(screen: Any, sources: list[SourceChoice], selected_index: int) -> int:
    query = ""
    matches = _matching_sources(sources, query)
    cursor = next(
        (
            index
            for index, (source_index, _source) in enumerate(matches)
            if source_index == selected_index
        ),
        0,
    )
    while True:
        _draw_source_menu(screen, matches, query, cursor, selected_index)
        key = screen.get_wch()
        if key in (curses.KEY_UP,):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN,):
            cursor = min(max(0, len(matches) - 1), cursor + 1)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if matches:
                return matches[cursor][0]
            curses.beep()
        elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            query = query[:-1]
            matches = _matching_sources(sources, query)
            cursor = 0
        elif key in ("\x1b", "\x03"):
            return selected_index
        elif isinstance(key, str) and key.isprintable():
            query += key
            matches = _matching_sources(sources, query)
            cursor = 0


def _fallback_choices(
    models: list[dict], selected: list[str], source: str, links: dict[str, str]
) -> list[dict]:
    choices = []
    # Edges outside an account-scoped view remain valid and are preserved on save.
    allowed = set(selected) | set(links) | set(links.values())
    for model in models:
        target = _selection_id(model)
        if target not in selected:
            continue
        try:
            validate_links({**links, source: target}, allowed)
        except ValueError:
            continue
        choices.append(model)
    return choices


def _fallback_menu(
    screen: Any, models: list[dict], source: dict, selected: list[str], links: dict[str, str]
) -> None:
    source_id = _selection_id(source)
    candidates = _fallback_choices(models, selected, source_id, links)
    query, cursor = "", 0
    while True:
        matches = top_matches(candidates, query)
        choices = [None, *matches]
        screen.erase()
        height, width = screen.getmaxyx()
        screen.addnstr(
            0,
            0,
            f"Fallback for {compact_model_name(source)}",
            width - 1,
            _style(PAIR_TITLE, curses.A_BOLD),
        )
        screen.addnstr(
            1, 0, str(source.get("display_source") or source.get("credential") or ""), width - 1
        )
        screen.addnstr(3, 0, "Search: " + query, width - 1, _style(PAIR_QUERY))
        screen.addnstr(
            4,
            0,
            "Type to filter · ↑/↓ move · Enter apply · Esc return · cycles excluded",
            width - 1,
        )
        start = max(0, cursor - max(1, height - 8) + 1)
        for row, model in enumerate(choices[start : start + max(1, height - 8)], 6):
            label = "No fallback"
            if model is not None:
                source_label = (
                    model.get("display_source")
                    or model.get("description")
                    or model.get("credential")
                )
                label = f"{compact_model_name(model)} — {source_label}"
            screen.addnstr(
                row,
                0,
                label,
                width - 1,
                _style(PAIR_CURSOR if row - 6 + start == cursor else PAIR_ACCENT),
            )
        screen.refresh()
        key = screen.get_wch()
        if key == curses.KEY_UP:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_DOWN:
            cursor = min(len(choices) - 1, cursor + 1)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if choices[cursor] is None:
                links.pop(source_id, None)
            else:
                links[source_id] = _selection_id(choices[cursor])
            return
        elif key in ("\x1b", "\x03"):
            return
        elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            query, cursor = query[:-1], 0
        elif isinstance(key, str) and key.isprintable():
            query, cursor = query + key, 0


def _curses_picker(
    models: list[dict[str, Any]],
    initial: list[str],
    initial_account: str | None = None,
    fallbacks: dict[str, str] | None = None,
    other_favorites: list[dict] | None = None,
) -> list[str] | None:
    links = dict(fallbacks or {})
    external = [
        model
        for model in (other_favorites or [])
        if all(_selection_id(model) != _selection_id(visible) for visible in models)
    ]

    def save(selected: list[str]) -> list[str]:
        if fallbacks is not None:
            fallbacks.clear()
            fallbacks.update(links)
        return selected

    def run(screen: Any) -> list[str] | None:
        nonlocal links
        _init_colors()
        curses.raw()
        screen.keypad(True)
        selected = [
            model_id
            for model_id in initial
            if any(_selection_id(model) == model_id for model in models)
        ]
        sources = _source_choices(models)
        source_index = _initial_source_index(sources, initial_account)
        query = ""
        results = _source_results(models, query, sources[source_index][0])
        cursor = 0
        search_mode = True
        while True:
            _draw(
                screen,
                results,
                query,
                cursor,
                selected,
                search_mode,
                models,
                sources[source_index][1],
                links,
            )
            key = screen.get_wch()
            if key == "\x06":
                if not search_mode and results and _selection_id(results[cursor]) in selected:
                    _fallback_menu(
                        screen,
                        models + external,
                        results[cursor],
                        selected + [_selection_id(model) for model in external],
                        links,
                    )
                else:
                    curses.beep()
                continue
            if key in ("\t", curses.KEY_BTAB):
                source_index = _source_menu(screen, sources, source_index)
                results = _source_results(models, query, sources[source_index][0])
                cursor = 0
                continue
            if search_mode:
                if key in ("\x13", "S"):
                    if selected:
                        return save(selected)
                    curses.beep()
                elif key in ("\n", "\r", curses.KEY_ENTER, curses.KEY_DOWN):
                    if results:
                        search_mode = False
                        cursor = 0
                    else:
                        curses.beep()
                elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                    query = query[:-1]
                    results = _source_results(models, query, sources[source_index][0])
                    cursor = 0
                elif key == "\x03":
                    return None
                elif key == "\x1b":
                    continue
                elif isinstance(key, str) and key.isprintable():
                    query += key
                    results = _source_results(models, query, sources[source_index][0])
                    cursor = 0
                continue

            if key in (curses.KEY_UP, "k"):
                if cursor == 0:
                    search_mode = True
                else:
                    cursor -= 1
            elif key in (curses.KEY_DOWN, "j"):
                cursor = min(max(0, len(results) - 1), cursor + 1)
            elif key in ("\n", "\r", " ", curses.KEY_ENTER) and results:
                model_id = _selection_id(results[cursor])
                _ordered_toggle(selected, model_id)
                if model_id not in selected:
                    links = {
                        source: target
                        for source, target in links.items()
                        if model_id not in (source, target)
                    }
            elif key == "/":
                query = ""
                results = _source_results(models, query, sources[source_index][0])
                cursor = 0
                search_mode = True
            elif key == "\x1b":
                search_mode = True
            elif key in ("s", "S"):
                if selected:
                    return save(selected)
                curses.beep()
            elif key in ("q", "Q", "\x03"):
                return None

    return curses.wrapper(run)


def _line_picker(
    models: list[dict[str, Any]],
    initial: list[str],
    initial_account: str | None = None,
    fallbacks: dict[str, str] | None = None,
    other_favorites: list[dict] | None = None,
) -> list[str] | None:
    links = dict(fallbacks or {})
    external = [
        model
        for model in (other_favorites or [])
        if all(_selection_id(model) != _selection_id(visible) for visible in models)
    ]
    selected = [
        model_id
        for model_id in initial
        if any(_selection_id(model) == model_id for model in models)
    ]
    sources = _source_choices(models)
    source_index = _initial_source_index(sources, initial_account)
    while True:
        print(f"Source: {sources[source_index][1]}")
        query = input("Search models (q cancels): ").strip()
        if query.lower() == "q":
            return None
        results = _source_results(models, query, sources[source_index][0])[:12]
        if not results:
            print("No matches.")
            continue
        for index, model in enumerate(results, 1):
            model_id = _selection_id(model)
            mark = "x" if model_id in selected else " "
            label = compact_model_name(model)
            source = model.get("display_source") or model.get("description")
            suffix = f" — {source}" if isinstance(source, str) and source else ""
            print(f"  {index:>2}) [{mark}] {label}{suffix}")
        while True:
            action = (
                input(
                    "Toggle number(s), b NUMBER fallback, f sources, / search, s save, q cancel: "
                )
                .strip()
                .lower()
            )
            if action == "f":
                source_index = _line_source_menu(sources, source_index)
                break
            if action == "/":
                break
            if action == "q":
                return None
            if action == "s":
                if selected:
                    if fallbacks is not None:
                        fallbacks.clear()
                        fallbacks.update(links)
                    return selected
                print("Select at least one model.", file=sys.stderr)
                continue
            try:
                if action.startswith("b "):
                    source = _selection_id(results[int(action[2:]) - 1])
                    if source not in selected:
                        raise ValueError
                    candidates = _fallback_choices(
                        models + external,
                        selected + [_selection_id(model) for model in external],
                        source,
                        links,
                    )
                    query = input("Search fallback models: ").strip()
                    candidates = top_matches(candidates, query)
                    print("0) No fallback")
                    for index, model in enumerate(candidates, 1):
                        source_label = model.get("display_source") or model.get("credential")
                        print(f"{index}) {compact_model_name(model)} — {source_label}")
                    index = int(input("Fallback number: "))
                    if index == 0:
                        links.pop(source, None)
                    elif 1 <= index <= len(candidates):
                        links[source] = _selection_id(candidates[index - 1])
                    else:
                        raise ValueError
                    continue
                indices = [int(value) for value in action.replace(",", " ").split()]
                if not indices:
                    raise ValueError
                for index in indices:
                    model_id = _selection_id(results[index - 1])
                    _ordered_toggle(selected, model_id)
                    if model_id not in selected:
                        links = {
                            source: target
                            for source, target in links.items()
                            if model_id not in (source, target)
                        }
            except (ValueError, IndexError):
                print("Enter result numbers, f, /, s, or q.", file=sys.stderr)


def _line_source_menu(sources: list[SourceChoice], selected_index: int) -> int:
    while True:
        query = input("Search accounts and keys (Enter shows all, q returns): ").strip()
        if query.casefold() == "q":
            return selected_index
        matches = _matching_sources(sources, query)
        if not matches:
            print("No matching accounts or keys.")
            continue
        for display_index, (source_index, (_source_id, label)) in enumerate(matches, 1):
            mark = "*" if source_index == selected_index else " "
            print(f"  {display_index:>2}) [{mark}] {label}")
        action = input("Source number (q returns): ").strip().lower()
        if action == "q":
            return selected_index
        try:
            index = int(action) - 1
            if not 0 <= index < len(matches):
                raise IndexError
            return matches[index][0]
        except (ValueError, IndexError):
            print("Enter a source number or q.", file=sys.stderr)


def choose_models(
    models: list[dict[str, Any]],
    initial: list[str],
    *,
    initial_account: str | None = None,
    fallbacks: dict[str, str] | None = None,
    other_favorites: list[dict] | None = None,
) -> list[str] | None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("interactive selection needs a terminal; pass routes as arguments")
    try:
        return _curses_picker(models, initial, initial_account, fallbacks, other_favorites)
    except curses.error:
        return _line_picker(models, initial, initial_account, fallbacks, other_favorites)
