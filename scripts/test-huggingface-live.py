"""Explicit live smoke check of catalog-advertised free routes; no config changes."""

import argparse
import http.client
import json
import threading

from claude_auth_manager.huggingface import fetch_models
from claude_auth_manager.models import managed_model
from claude_auth_manager.proxy import LOCAL_TOKEN_HEADER, HybridRouterServer
from claude_auth_manager.registry import read_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="test only this exact free provider route")
    parser.add_argument("--tools", action="store_true", help="test a harmless echo tool round trip")
    args = parser.parse_args()
    models = [
        dict(model, provider="huggingface", credential="hf")
        for model in fetch_models(read_key("hf", provider="huggingface"))
    ]
    if args.model:
        models = [model for model in models if model["id"] == args.model]
        if not models:
            raise SystemExit("Requested model is not currently listed as free")
    server = HybridRouterServer(
        ("127.0.0.1", 0),
        local_token="synthetic-probe",
        routes={managed_model(model): model for model in models},
        fallbacks={},
        record_status=False,
        fallback_timeout=30,
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        for model in models:
            messages = [{"role": "user", "content": "Reply with OK."}]
            extra = {}
            if args.tools:
                messages = [
                    {"role": "user", "content": "Call echo with text OK, then report its result."}
                ]
                extra = {
                    "tools": [
                        {
                            "name": "echo",
                            "input_schema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ],
                    "tool_choice": {"type": "tool", "name": "echo"},
                }
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=60)
            connection.request(
                "POST",
                "/v1/messages",
                json.dumps(
                    {
                        "model": managed_model(model),
                        "max_tokens": 512,
                        "messages": messages,
                        **extra,
                    }
                ),
                {LOCAL_TOKEN_HEADER: "synthetic-probe", "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            if args.tools:
                calls = [
                    block for block in result.get("content", []) if block.get("type") == "tool_use"
                ]
                assert calls and calls[0]["name"] == "echo", "No echo tool call received"
                messages.extend(
                    [
                        {"role": "assistant", "content": result["content"]},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": calls[0]["id"],
                                    "content": "OK",
                                }
                            ],
                        },
                    ]
                )
                connection.request(
                    "POST",
                    "/v1/messages",
                    json.dumps(
                        {
                            "model": managed_model(model),
                            "max_tokens": 512,
                            "messages": messages,
                        }
                    ),
                    {LOCAL_TOKEN_HEADER: "synthetic-probe", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                result = json.loads(response.read())
                assert response.status == 200 and result.get("content"), (
                    "Tool result round-trip failed"
                )
            print(
                json.dumps(
                    {
                        "model": model["id"],
                        "http_status": response.status,
                        "has_content": bool(result.get("content")),
                        "tool_round_trip": args.tools,
                    }
                ),
                flush=True,
            )
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
