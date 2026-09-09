from __future__ import annotations

import json

from claude_auth_manager.google import (
    anthropic_to_openai,
    approximate_input_tokens,
    fetch_models,
    iter_openai_sse,
    openai_to_anthropic,
)


def _events(chunks: list[bytes]) -> list[tuple[str, dict]]:
    result: list[tuple[str, dict]] = []
    for chunk in chunks:
        event = ""
        data = ""
        for line in chunk.decode().splitlines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        result.append((event, json.loads(data)))
    return result


def test_anthropic_request_translation_preserves_images_tools_and_tool_results() -> None:
    source = {
        "model": "gemini-3.8-flash",
        "system": [{"type": "text", "text": "Be exact."}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "aGVsbG8=",
                        },
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "/tmp/a"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "contents"}],
                    },
                    {"type": "text", "text": "Continue"},
                ],
            },
        ],
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "examples": ["/tmp/a"]},
                        "lines": {"type": "array"},
                    },
                    "required": ["path"],
                },
            }
        ],
        "tool_choice": {"type": "tool", "name": "read_file"},
        "max_tokens": 512,
        "temperature": 0.2,
        "stop_sequences": ["STOP"],
        "stream": True,
        "output_config": {"effort": "high"},
    }

    translated = anthropic_to_openai(source)

    assert translated["model"] == "gemini-3.8-flash"
    assert translated["stream_options"] == {"include_usage": True}
    assert translated["messages"][0] == {"role": "system", "content": "Be exact."}
    image = translated["messages"][1]["content"][1]
    assert image["image_url"]["url"] == "data:image/png;base64,aGVsbG8="
    assistant = translated["messages"][2]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path":"/tmp/a"}'
    assert translated["messages"][3] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": "contents",
    }
    assert translated["messages"][4] == {"role": "user", "content": "Continue"}
    parameters = translated["tools"][0]["function"]["parameters"]
    assert "$schema" not in parameters
    assert "examples" not in parameters["properties"]["path"]
    assert parameters["properties"]["lines"]["items"] == {"type": "string"}
    assert translated["tool_choice"]["function"]["name"] == "read_file"
    assert translated["max_completion_tokens"] == 512
    assert translated["reasoning_effort"] == "high"


def test_openai_response_translation_returns_text_tool_use_usage_and_stop_reason() -> None:
    metadata: dict[str, dict] = {}
    translated = openai_to_anthropic(
        {
            "id": "chatcmpl_1",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "Checking",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "extra_content": {
                                    "google": {"thought_signature": "encrypted-state"}
                                },
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"query":"weather"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        },
        "gemini-test",
        metadata,
    )

    assert translated["id"] == "chatcmpl_1"
    assert translated["model"] == "gemini-test"
    assert translated["content"] == [
        {"type": "text", "text": "Checking"},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "lookup",
            "input": {"query": "weather"},
        },
    ]
    assert translated["stop_reason"] == "tool_use"
    assert translated["usage"] == {"input_tokens": 12, "output_tokens": 7}
    assert metadata == {"call_1": {"google": {"thought_signature": "encrypted-state"}}}

    replayed = anthropic_to_openai(
        {
            "model": "gemini-test",
            "messages": [
                {"role": "assistant", "content": translated["content"]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": "sunny",
                        }
                    ],
                },
            ],
        },
        metadata,
    )
    assert replayed["messages"][0]["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "encrypted-state"}
    }


def test_openai_stream_translation_emits_valid_anthropic_text_and_tool_events() -> None:
    def data(value: dict) -> bytes:
        return b"data: " + json.dumps(value, separators=(",", ":")).encode() + b"\n\n"

    upstream = [
        data({"choices": [{"delta": {"content": "Hi "}, "finish_reason": None}]}),
        data({"choices": [{"delta": {"content": "there"}, "finish_reason": None}]}),
        data(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "extra_content": {
                                        "google": {"thought_signature": "stream-state"}
                                    },
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        data(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
            }
        ),
        b"data: [DONE]\n\n",
    ]

    wire = b"".join(upstream)
    metadata: dict[str, dict] = {}
    events = _events(list(iter_openai_sse(wire.splitlines(keepends=True), "gemini-test", metadata)))
    names = [name for name, _ in events]

    assert names[0] == "message_start"
    assert names.count("content_block_start") == 2
    assert names.count("content_block_stop") == 2
    text = [
        data["delta"]["text"]
        for name, data in events
        if name == "content_block_delta" and data["delta"]["type"] == "text_delta"
    ]
    assert text == ["Hi ", "there"]
    partials = [
        data["delta"]["partial_json"]
        for name, data in events
        if name == "content_block_delta" and data["delta"]["type"] == "input_json_delta"
    ]
    assert "".join(partials) == '{"q":"x"}'
    message_delta = next(data for name, data in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"
    assert message_delta["usage"]["output_tokens"] == 4
    assert names[-1] == "message_stop"
    assert metadata == {"call_1": {"google": {"thought_signature": "stream-state"}}}


def test_fetch_models_filters_non_gemini_and_deduplicates(monkeypatch) -> None:
    monkeypatch.setattr(
        "claude_auth_manager.google._request_json",
        lambda _path, _key: {
            "data": [
                {"id": "models/gemini-z", "name": "Z"},
                {"id": "embedding-1"},
                {"id": "gemini-embedding-2-preview"},
                {"id": "gemini-2.5-flash-image"},
                {"id": "gemini-a", "owned_by": "google"},
                {"id": "gemini-z", "name": "duplicate"},
                {"bad": True},
            ]
        },
    )
    models = fetch_models("private-key")
    assert [model["id"] for model in models] == ["gemini-a", "gemini-z"]
    assert all(model["architecture"]["input_modalities"] == ["text", "image"] for model in models)


def test_approximate_token_count_is_positive_and_scales() -> None:
    assert approximate_input_tokens({"messages": []}) >= 1
    assert approximate_input_tokens({"messages": [{"content": "x" * 1000}]}) > 200
