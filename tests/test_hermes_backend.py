"""
Unit checks for HermesBackend — no Hermes server or device required.

Covers the parts most likely to break the voice path:
1. SSE parsing: a mixed stream of content chunks + hermes.tool.progress events
   yields ONLY spoken content deltas (progress events never reach TTS).
2. Header construction: bearer auth + per-device X-Hermes-Session-Key.
3. session_id rotates on reset() while session_key (long-term memory) stays put.
4. ${ENV_VAR} api_key resolution.

Run:
    python tests/test_hermes_backend.py
Exits non-zero on the first failed check.
"""
import asyncio
import json
import os
import sys

from pipeline.conversation.hermes import HermesBackend


def _cfg(**over):
    cfg = {
        "base_url": "http://127.0.0.1:8642/v1",
        "api_key": "test-key",
        "model": "hermes-agent",
        "voice_prompt": "[voice prompt]",
    }
    cfg.update(over)
    return cfg


async def _lines(*items):
    for item in items:
        yield item


def _content_chunk(text):
    return "data: " + json.dumps({
        "id": "x", "object": "chat.completion.chunk", "created": 0, "model": "hermes-agent",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    })


async def test_sse_filters_tool_progress():
    backend = HermesBackend(_cfg(), device_id="dev1")
    # Simulate the exact wire shape Hermes emits: role chunk, content, a
    # tool.progress event (event: line + data: line), more content, finish, DONE.
    lines = _lines(
        'data: {"object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}',
        "",
        _content_chunk("Hello"),
        "",
        "event: hermes.tool.progress",
        'data: {"message_id":"m","tool_name":"web_search","delta":"searching"}',
        "",
        _content_chunk(" world"),
        "",
        ": keepalive",
        'data: {"object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    )
    out = [chunk async for chunk in backend._iter_sse_content(lines)]
    assert out == ["Hello", " world"], out
    print("PASS: SSE parsing filters tool.progress and yields only content")


async def test_headers_and_session():
    backend = HermesBackend(_cfg(), device_id="kitchen")
    headers = backend._headers()
    assert headers["X-Hermes-Session-Key"] == "agent:onju:voice:kitchen", headers
    assert headers["X-Hermes-Session-Id"].startswith("onju-kitchen-"), headers

    first_id = backend.session_id
    backend.reset()
    assert backend.session_id != first_id, "session_id should rotate on reset()"
    assert backend.session_key == "agent:onju:voice:kitchen", "session_key must stay stable"
    print("PASS: headers + session-id rotation / stable memory key")


async def test_env_resolution():
    os.environ["HERMES_TEST_KEY"] = "resolved-secret"
    backend = HermesBackend(_cfg(api_key="${HERMES_TEST_KEY}"), device_id="d")
    assert backend.api_key == "resolved-secret", backend.api_key
    print("PASS: ${ENV_VAR} api_key resolution")


async def main():
    await test_sse_filters_tool_progress()
    await test_headers_and_session()
    await test_env_resolution()
    print("\nAll HermesBackend checks passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
