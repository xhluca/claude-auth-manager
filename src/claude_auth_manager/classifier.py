"""Opt-in credential-only routing for native Sonnet, including permission checks."""

from typing import Any


def accounts(document: dict[str, Any]) -> list[str]:
    values = document.get("classifier_accounts", [])
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ValueError("classifier_accounts must be a list of account IDs")
    if len(set(values)) != len(values):
        raise ValueError("classifier account order cannot contain duplicates")
    return list(values)


def matches(model: str) -> bool:
    # Match model identifiers, never heuristics over the permission-check prompt.
    return model.removesuffix("[1m]") == "sonnet" or model.startswith("claude-sonnet-")


def routes(model: str, selected: list[str]) -> dict[str, str]:
    return {f"classifier/{account}/{model}": account for account in selected}
