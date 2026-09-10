from __future__ import annotations

import curses
from typing import Any

from claude_auth_manager import picker


class FakeScreen:
    def __init__(self, keys: list[Any]) -> None:
        self.keys = keys

    def keypad(self, _enabled: bool) -> None:
        pass

    def get_wch(self) -> Any:
        return self.keys.pop(0)


class RecordingScreen:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.writes: list[tuple[int, str]] = []

    def erase(self) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return (24, 100)

    def addnstr(self, row: int, _column: int, value: str, _limit: int, _style: int = 0) -> None:
        self.values.append(value)
        self.writes.append((row, value))

    def move(self, _row: int, _column: int) -> None:
        pass

    def refresh(self) -> None:
        pass


def run_picker(monkeypatch, sample_models, keys):
    states: list[tuple[str, int, bool]] = []
    screen = FakeScreen(keys)

    def record_draw(
        _screen,
        _models,
        query,
        cursor,
        _selected,
        search_mode,
        _catalog=None,
        _source_label="All accounts and keys",
        _fallbacks=None,
    ) -> None:
        states.append((query, cursor, search_mode))

    monkeypatch.setattr(picker, "_init_colors", lambda: None)
    monkeypatch.setattr(picker.curses, "raw", lambda: None)
    monkeypatch.setattr(picker.curses, "wrapper", lambda run: run(screen))
    monkeypatch.setattr(picker, "_draw", record_draw)
    result = picker._curses_picker(sample_models, [])
    return result, states


def test_down_from_search_focuses_first_result(monkeypatch, sample_models) -> None:
    result, states = run_picker(monkeypatch, sample_models, ["c", curses.KEY_DOWN, "q"])

    assert result is None
    assert states[-1] == ("c", 0, False)


def test_up_past_first_result_returns_to_search(monkeypatch, sample_models) -> None:
    result, states = run_picker(
        monkeypatch,
        sample_models,
        ["c", curses.KEY_DOWN, curses.KEY_UP, "\x03"],
    )

    assert result is None
    assert states[-1] == ("c", 0, True)


def test_escape_from_results_returns_to_search(monkeypatch, sample_models) -> None:
    result, states = run_picker(
        monkeypatch,
        sample_models,
        ["c", curses.KEY_DOWN, "\x1b", "\x03"],
    )

    assert result is None
    assert states[-1] == ("c", 0, True)


def test_control_s_saves_while_search_has_focus(monkeypatch, sample_models) -> None:
    result, _states = run_picker(
        monkeypatch,
        sample_models,
        [*"sonnet", curses.KEY_DOWN, " ", "\x1b", "\x13"],
    )

    assert result == ["anthropic/claude-sonnet-4.6"]


def test_shift_s_saves_while_search_has_focus(monkeypatch, sample_models) -> None:
    result, _states = run_picker(
        monkeypatch,
        sample_models,
        [*"sonnet", curses.KEY_DOWN, " ", "\x1b", "S"],
    )

    assert result == ["anthropic/claude-sonnet-4.6"]


def test_lowercase_s_remains_available_for_search(monkeypatch, sample_models) -> None:
    result, states = run_picker(monkeypatch, sample_models, ["s", "\x03"])

    assert result is None
    assert states[-1] == ("s", 0, True)


def test_fallback_menu_searches_selected_routes_and_allows_cycles(monkeypatch):
    class MenuScreen(RecordingScreen):
        def __init__(self):
            super().__init__()
            self.keys = iter([*"gamma", "\n", "\x13"])

        def get_wch(self):
            return next(self.keys)

    models = [
        {"id": name, "name": name, "selection_id": name, "credential": name}
        for name in ("alpha", "beta", "gamma", "inactive")
    ]
    links = {"beta": ["alpha"]}
    selected = ["alpha", "beta", "gamma"]
    candidates = picker._fallback_choices(models, selected, "alpha", links)
    assert [model["id"] for model in candidates] == ["beta", "gamma"]
    monkeypatch.setattr(picker, "_COLORS_ENABLED", False)
    screen = MenuScreen()
    picker._fallback_menu(screen, models, models[0], selected, links)
    assert links == {"beta": ["alpha"], "alpha": ["gamma"]}
    assert any("gamma" in value for value in screen.values)


def test_control_f_edits_are_committed_only_when_picker_is_saved(monkeypatch):
    models = [{"id": name, "name": name} for name in ("alpha", "beta")]
    monkeypatch.setattr(picker, "_init_colors", lambda: None)
    monkeypatch.setattr(picker.curses, "raw", lambda: None)
    monkeypatch.setattr(picker, "_draw", lambda *_args: None)
    monkeypatch.setattr(
        picker,
        "_fallback_menu",
        lambda _screen, _models, _source, _selected, links: links.update(alpha=["beta"]),
    )
    links = {}
    for action in ("q", "s"):
        screen = FakeScreen([curses.KEY_DOWN, "\x06", action])
        monkeypatch.setattr(picker.curses, "wrapper", lambda run, screen=screen: run(screen))
        result = picker._curses_picker(models, ["alpha", "beta"], fallbacks=links)
        if action == "q":
            assert result is None and links == {}
        else:
            assert result == ["alpha", "beta"] and links == {"alpha": ["beta"]}


def test_fallback_menu_reorders_and_cancels(monkeypatch):
    models = [{"id": name, "name": name} for name in ("alpha", "beta", "gamma")]
    monkeypatch.setattr(picker, "_COLORS_ENABLED", False)
    for finish, expected in [("\x13", ["gamma", "beta"]), ("\x1b", ["beta", "gamma"])]:
        screen = RecordingScreen()
        keys = iter(["\t", curses.KEY_DOWN, "\x15", finish])
        screen.get_wch = lambda keys=keys: next(keys)
        links = {"alpha": ["beta", "gamma"]}
        picker._fallback_menu(screen, models, models[0], ["alpha", "beta", "gamma"], links)
        assert links["alpha"] == expected


def test_search_help_shows_save_shortcuts(monkeypatch, sample_models) -> None:
    screen = RecordingScreen()
    monkeypatch.setattr(picker, "_set_cursor", lambda _visible: None)
    monkeypatch.setattr(picker, "_COLORS_ENABLED", False)

    picker._draw(screen, sample_models, "sonnet", 0, [], True)

    assert any("Ctrl-S/Shift-S save" in value for value in screen.values)
    assert any("[Tab to change]" in value for value in screen.values)
    assert any(row == 2 and value == "Search: " for row, value in screen.writes)
    assert any(row == 3 and value == "Source: " for row, value in screen.writes)


def test_tab_opens_source_menu_without_losing_hidden_selections(monkeypatch) -> None:
    models = [
        {
            "id": "claude-sonnet-5",
            "selection_id": "cam/anthropic/one@example.com/claude-sonnet-5",
            "provider": "anthropic",
            "credential": "one@example.com",
            "credential_label": "One",
            "supported_parameters": ["tools"],
        },
        {
            "id": "claude-sonnet-5",
            "selection_id": "cam/anthropic/two@example.com/claude-sonnet-5",
            "provider": "anthropic",
            "credential": "two@example.com",
            "credential_label": "Two",
            "supported_parameters": ["tools"],
        },
    ]

    monkeypatch.setattr(picker, "_draw_source_menu", lambda *_args: None)
    result, _states = run_picker(
        monkeypatch,
        models,
        [
            "\t",
            curses.KEY_DOWN,
            "\n",
            curses.KEY_DOWN,
            " ",
            "\t",
            curses.KEY_DOWN,
            "\n",
            " ",
            "s",
        ],
    )

    assert result == [
        "cam/anthropic/one@example.com/claude-sonnet-5",
        "cam/anthropic/two@example.com/claude-sonnet-5",
    ]


def test_picker_draws_active_source_filter(monkeypatch, sample_models) -> None:
    screen = RecordingScreen()
    monkeypatch.setattr(picker, "_set_cursor", lambda _visible: None)
    monkeypatch.setattr(picker, "_COLORS_ENABLED", False)

    picker._draw(screen, sample_models, "", 0, [], True, source_label="OpenRouter · API key (work)")

    rendered = "\n".join(screen.values)
    assert "Source: " in rendered
    assert "OpenRouter · API key (work)" in rendered


def test_line_picker_can_search_source_filters(monkeypatch, capsys) -> None:
    models = [
        {
            "id": "claude-sonnet-5",
            "provider": "anthropic",
            "credential": "work@example.com",
            "credential_label": "Work",
            "supported_parameters": ["tools"],
        }
    ]
    answers = iter(["", "f", "work", "1", "", "1", "s"])
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))

    assert picker._line_picker(models, []) == ["claude-sonnet-5"]
    output = capsys.readouterr().out
    assert "Source: Claude ·" in output
    assert "(Work)" in output


def test_source_choices_include_subscriptions_and_provider_keys() -> None:
    models = [
        {
            "id": "claude-opus-5",
            "provider": "anthropic",
            "credential": "person@example.com",
            "credential_label": "person@example.com",
            "subscription": "max",
            "rate_limit_tier": "default_claude_max_20x",
        },
        {
            "id": "vendor/model",
            "provider": "openrouter",
            "credential": "personal",
        },
        {"id": "gemini-model", "provider": "google", "credential": "lab"},
        {
            "id": "claude-api-model",
            "provider": "anthropic-api",
            "credential": "metered",
        },
    ]

    assert picker._source_choices(models) == [
        (None, "All accounts and keys"),
        (("anthropic", "person@example.com"), "Claude · Max 20x (person@example.com)"),
        (("openrouter", "personal"), "OpenRouter · API key (personal)"),
        (("google", "lab"), "Google · Gemini API (lab)"),
        (("anthropic-api", "metered"), "Claude · Anthropic API (metered)"),
    ]


def test_source_results_filter_exact_credential() -> None:
    models = [
        {"id": "one", "provider": "openrouter", "credential": "first"},
        {"id": "two", "provider": "openrouter", "credential": "second"},
    ]

    assert picker._source_results(models, "", ("openrouter", "second")) == [models[1]]


def test_source_menu_searches_accounts_and_keys(monkeypatch) -> None:
    sources: list[picker.SourceChoice] = [
        (None, "All accounts and keys"),
        (("anthropic", "person@example.com"), "Claude · Max 20x (person@example.com)"),
        (("openrouter", "personal"), "OpenRouter · API key (personal)"),
    ]
    screen = FakeScreen([*"router", "\n"])
    monkeypatch.setattr(picker, "_draw_source_menu", lambda *_args: None)

    assert picker._source_menu(screen, sources, 0) == 2


def test_picker_omits_tool_badges_and_selected_warning(monkeypatch, sample_models) -> None:
    screen = RecordingScreen()
    monkeypatch.setattr(picker, "_set_cursor", lambda _visible: None)
    monkeypatch.setattr(picker, "_COLORS_ENABLED", False)

    picker._draw(
        screen,
        sample_models[2:],
        "",
        0,
        ["qwen/qwen3-coder"],
        True,
        sample_models,
    )

    rendered = "\n".join(screen.values)
    assert "[tools" not in rendered
    assert "without advertised tools" not in rendered


def test_managed_picker_shows_normal_name_and_account_not_internal_route(monkeypatch) -> None:
    screen = RecordingScreen()
    monkeypatch.setattr(picker, "_set_cursor", lambda _visible: None)
    monkeypatch.setattr(picker, "_COLORS_ENABLED", False)
    model = {
        "id": "claude-opus-4-8",
        "name": "Claude Opus 4.8",
        "selection_id": "cam/anthropic/account-example-com/claude-opus-4-8",
        "display_source": ("Claude · Max 20x (account@example.com) via claude-auth-manager"),
        "provider": "anthropic",
        "supported_parameters": ["tools"],
    }

    picker._draw(screen, [model], "", 0, [], True)

    rendered = "\n".join(screen.values)
    assert "Opus 4.8" in rendered
    assert "Claude Opus" not in rendered
    assert "Claude · Max 20x (account@example.com) via claude-auth-manager" in rendered
    assert "account@example.com" in rendered
    assert "cam/anthropic" not in rendered
    assert "account-example-com" not in rendered
