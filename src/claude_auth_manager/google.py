"""Direct Gemini API catalog access and Anthropic/OpenAI protocol translation."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .paths import catalog_path
from .storage import atomic_write_json, read_json_object

DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
NON_CHAT_MODEL_MARKERS = (
    "embedding",
    "-image",
    "-tts",
    "-native-audio",
    "-live",
    "-omni",
    "transcribe",
)
MAX_TOOL_METADATA_ENTRIES = 4096


def api_base() -> str:
    return os.environ.get("CLAUDE_AUTH_MANAGER_GOOGLE_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _request_json(path: str, key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_base()}/{path.lstrip('/')}",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "claude-auth-manager/0.1",
            "x-goog-api-client": "claude-auth-manager/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode(errors="replace")
        except OSError:
            detail = ""
        raise RuntimeError(
            f"Google rejected the request (HTTP {exc.code}): {detail[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Google: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Google returned an invalid JSON response")
    return payload


def fetch_models(key: str) -> list[dict[str, Any]]:
    payload = _request_json("models", key)
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("Google returned an invalid model catalog")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_model_id = item.get("id")
        model_id = raw_model_id.removeprefix("models/") if isinstance(raw_model_id, str) else None
        if (
            not isinstance(model_id, str)
            or not model_id.startswith("gemini-")
            or any(marker in model_id.casefold() for marker in NON_CHAT_MODEL_MARKERS)
            or model_id in seen
        ):
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "name": str(item.get("name") or model_id),
                "description": "Direct Google Gemini API",
                "owned_by": item.get("owned_by", "google"),
                "supported_parameters": ["tools", "tool_choice", "images"],
                "architecture": {"input_modalities": ["text", "image"]},
            }
        )
    if not models:
        raise RuntimeError("Google returned no Gemini chat models")
    return sorted(models, key=lambda item: str(item["id"]))


def refresh_catalog(key_id: str, key: str) -> list[dict[str, Any]]:
    models = fetch_models(key)
    atomic_write_json(
        catalog_path("google", key_id),
        {
            "version": 1,
            "provider": "google",
            "credential": key_id,
            "source": f"{api_base()}/models",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "models": models,
        },
    )
    return models


def load_catalog(key_id: str) -> list[dict[str, Any]]:
    path = catalog_path("google", key_id)
    try:
        document = read_json_object(path)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Google model index not found for {key_id}; run cam index") from exc
    models = document.get("models")
    if not isinstance(models, list):
        raise RuntimeError(f"invalid Google model index at {path}")
    return [
        model for model in models if isinstance(model, dict) and isinstance(model.get("id"), str)
    ]


def validate_key(key: str) -> None:
    fetch_models(key)


def _text_from_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, dict):
            parts.append(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(part for part in parts if part)


def _openai_content(blocks: Any) -> str | list[dict[str, Any]]:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return _text_from_blocks(blocks)
    content: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            content.append({"type": "text", "text": str(block)})
            continue
        kind = block.get("type")
        if kind == "text":
            content.append({"type": "text", "text": str(block.get("text", ""))})
        elif kind == "image":
            source = block.get("source")
            if isinstance(source, dict) and source.get("type") == "base64":
                media = str(source.get("media_type") or "application/octet-stream")
                data = str(source.get("data") or "")
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
                )
            elif isinstance(source, dict) and source.get("type") == "url":
                content.append(
                    {"type": "image_url", "image_url": {"url": str(source.get("url") or "")}}
                )
        elif kind not in {"thinking", "redacted_thinking", "tool_result", "tool_use"}:
            content.append({"type": "text", "text": _text_from_blocks([block])})
    if not content:
        return ""
    if all(part.get("type") == "text" for part in content):
        return "\n".join(str(part.get("text", "")) for part in content)
    return content


def _assistant_message(
    content: Any, tool_metadata: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    blocks = content if isinstance(content, list) else []
    message: dict[str, Any] = {"role": "assistant", "content": _openai_content(blocks)}
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        call_id = str(block.get("id") or f"call_{uuid.uuid4().hex}")
        translated_call: dict[str, Any] = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": str(block.get("name") or "tool"),
                "arguments": json.dumps(
                    block.get("input") if isinstance(block.get("input"), dict) else {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        }
        metadata = tool_metadata.get(call_id) if tool_metadata is not None else None
        if isinstance(metadata, dict):
            translated_call["extra_content"] = dict(metadata)
        tool_calls.append(translated_call)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _user_messages(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    blocks = content if isinstance(content, list) else []
    result: list[dict[str, Any]] = []
    ordinary: list[dict[str, Any]] = []

    def flush() -> None:
        if ordinary:
            result.append({"role": "user", "content": _openai_content(list(ordinary))})
            ordinary.clear()

    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            flush()
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or "unknown"),
                    "content": _text_from_blocks(block.get("content")),
                }
            )
        elif isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}:
            continue
        else:
            ordinary.append(
                block if isinstance(block, dict) else {"type": "text", "text": str(block)}
            )
    flush()
    return result or [{"role": "user", "content": ""}]


def _repair_schema(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _repair_schema(item)
        return
    if not isinstance(value, dict):
        return
    if str(value.get("type", "")).casefold() == "array" and "items" not in value:
        value["items"] = {"type": "string"}
    # Gemini/OpenAI compatibility ignores Anthropic cache controls and rejects
    # several draft-specific annotations in function declarations.
    for unsupported in ("cache_control", "$schema", "examples"):
        value.pop(unsupported, None)
    for item in value.values():
        _repair_schema(item)


def anthropic_to_openai(
    payload: dict[str, Any],
    tool_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _text_from_blocks(system)})
    configured = payload.get("messages")
    if not isinstance(configured, list):
        raise ValueError("messages must be an array")
    for message in configured:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            messages.append(_assistant_message(message.get("content"), tool_metadata))
        elif role == "user":
            messages.extend(_user_messages(message.get("content")))

    result: dict[str, Any] = {
        "model": payload["model"],
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }
    if result["stream"]:
        result["stream_options"] = {"include_usage": True}
    mapping = {
        "max_tokens": "max_completion_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
        "stop_sequences": "stop",
    }
    for source, target in mapping.items():
        if source in payload:
            result[target] = payload[source]
    thinking = payload.get("thinking")
    output = payload.get("output_config")
    effort = output.get("effort") if isinstance(output, dict) else None
    if isinstance(effort, str) and effort in {"low", "medium", "high"}:
        result["reasoning_effort"] = effort
    elif isinstance(thinking, dict) and thinking.get("type") == "disabled":
        result["reasoning_effort"] = "none"

    tools = payload.get("tools")
    if isinstance(tools, list):
        translated: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            parameters = tool.get("input_schema")
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            parameters = json.loads(json.dumps(parameters))
            _repair_schema(parameters)
            translated.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": str(tool.get("description") or ""),
                        "parameters": parameters,
                    },
                }
            )
        if translated:
            result["tools"] = translated
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "auto":
            result["tool_choice"] = "auto"
        elif choice_type == "any":
            result["tool_choice"] = "required"
        elif choice_type == "none":
            result["tool_choice"] = "none"
        elif choice_type == "tool" and isinstance(tool_choice.get("name"), str):
            result["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
    return result


def _usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        "input_tokens": int(source.get("prompt_tokens") or 0),
        "output_tokens": int(source.get("completion_tokens") or 0),
    }


def _tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments:
        return {}
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        return {"_raw": arguments}
    return value if isinstance(value, dict) else {"value": value}


def _store_tool_metadata(
    target: dict[str, dict[str, Any]], call_id: str, value: dict[str, Any]
) -> None:
    if call_id not in target and len(target) >= MAX_TOOL_METADATA_ENTRIES:
        oldest = next(iter(target), None)
        if oldest is not None:
            target.pop(oldest, None)
    target[call_id] = dict(value)


def openai_to_anthropic(
    payload: dict[str, Any],
    model: str,
    tool_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    choices = payload.get("choices")
    choice = (
        choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    )
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
            extra_content = call.get("extra_content") if isinstance(call, dict) else None
            if tool_metadata is not None and isinstance(extra_content, dict):
                _store_tool_metadata(tool_metadata, call_id, extra_content)
            content.append(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": str(function.get("name") or "tool"),
                    "input": _tool_input(function.get("arguments")),
                }
            )
    finish = choice.get("finish_reason")
    stop_reason = {
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
    }.get(finish, "end_turn")
    return {
        "id": str(payload.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _usage(payload.get("usage")),
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n".encode()


@dataclass
class OpenAIStreamTranslator:
    """Translate OpenAI-compatible SSE chunks into Anthropic message events."""

    model: str
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    started: bool = False
    next_block_index: int = 0
    text_block: int | None = None
    tool_blocks: dict[int, int] = field(default_factory=dict)
    open_blocks: set[int] = field(default_factory=set)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    tool_metadata: dict[str, dict[str, Any]] | None = None

    def _start(self) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        return [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": self.usage["input_tokens"], "output_tokens": 0},
                    },
                },
            )
        ]

    def _new_block(self, content_block: dict[str, Any]) -> tuple[int, bytes]:
        index = self.next_block_index
        self.next_block_index += 1
        self.open_blocks.add(index)
        return index, _sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": content_block},
        )

    def feed(self, payload: dict[str, Any]) -> list[bytes]:
        output = self._start()
        if isinstance(payload.get("usage"), dict):
            self.usage = _usage(payload["usage"])
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return output
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if isinstance(choice.get("finish_reason"), str):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self.text_block is None:
                    self.text_block, start = self._new_block({"type": "text", "text": ""})
                    output.append(start)
                output.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self.text_block,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )
            tool_calls = delta.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                source_index = call.get("index")
                source_index = (
                    source_index if isinstance(source_index, int) else len(self.tool_blocks)
                )
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                block_index = self.tool_blocks.get(source_index)
                if block_index is None:
                    call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
                    extra_content = call.get("extra_content")
                    if self.tool_metadata is not None and isinstance(extra_content, dict):
                        _store_tool_metadata(self.tool_metadata, call_id, extra_content)
                    block_index, start = self._new_block(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": str(function.get("name") or "tool"),
                            "input": {},
                        }
                    )
                    self.tool_blocks[source_index] = block_index
                    output.append(start)
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    output.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": block_index,
                                "delta": {"type": "input_json_delta", "partial_json": arguments},
                            },
                        )
                    )
        return output

    def finish(self) -> list[bytes]:
        output = self._start()
        for index in sorted(self.open_blocks):
            output.append(
                _sse("content_block_stop", {"type": "content_block_stop", "index": index})
            )
        stop_reason = {
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "refusal",
        }.get(self.finish_reason, "end_turn")
        output.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": self.usage["output_tokens"]},
                },
            )
        )
        output.append(_sse("message_stop", {"type": "message_stop"}))
        return output


def iter_openai_sse(
    lines: Iterable[bytes],
    model: str,
    tool_metadata: dict[str, dict[str, Any]] | None = None,
) -> Iterable[bytes]:
    translator = OpenAIStreamTranslator(model, tool_metadata=tool_metadata)
    data_lines: list[bytes] = []
    for raw in lines:
        line = raw.rstrip(b"\r\n")
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line or not data_lines:
            continue
        data = b"\n".join(data_lines)
        data_lines.clear()
        if data == b"[DONE]":
            break
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield from translator.feed(payload)
    yield from translator.finish()


def approximate_input_tokens(payload: dict[str, Any]) -> int:
    """Conservative local fallback for Claude's optional count_tokens request."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(encoded.encode("utf-8")) + 2) // 3)
