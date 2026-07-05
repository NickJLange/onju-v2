"""
Validates the SSE streaming path against a locally running hermes-agent API
server. No ESP32 / Onju device required.

What it checks:
1. The hermes backend's stream() actually yields content deltas progressively.
2. The sentence_chunks() splitter emits whole sentences as they form.
3. An "ack-first" prompt produces a short opening sentence that lands well
   before the full response, which is the behavior the pipeline relies on.
4. Raw SSE inspection — logs every event (content deltas, the custom
   hermes.tool.progress events, finish_reason, inter-event gaps) so you can
   see exactly what Hermes sends mid-turn.

Usage:
    python tests/test_stream.py                          # default tool-using prompt
    python tests/test_stream.py "your prompt here"
    python tests/test_stream.py --raw-only "prompt"      # skip sentence pass, only dump events

Reads pipeline/config.yaml for the Hermes base_url and api_key. Forces the
conversation backend to "hermes" regardless of what config.yaml has set.

Prereq: a Hermes API server reachable at hermes.base_url. On the Hermes host:
    API_SERVER_ENABLED=true API_SERVER_KEY=... hermes gateway
See docs/hermes-secure-setup.md for the secure (restricted-toolset) setup.
"""
import argparse
import asyncio
import json
import os
import re
import time

import httpx
import yaml

from pipeline.conversation import create_backend, sentence_chunks


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


DEFAULT_PROMPT = (
    "I'm going to need you to search the web for the latest news about the "
    "James Webb Space Telescope and then tell me the single most interesting "
    "finding in one sentence."
)

GAP_THRESHOLD = 1.0  # seconds — flag pauses longer than this


async def raw_event_inspection(cfg: dict, content: str) -> int:
    """Hit the Hermes SSE endpoint directly and log every event's shape —
    content deltas, hermes.tool.progress events, finish_reason, and gaps.
    Returns the number of tool.progress events seen."""
    print("--- raw SSE inspection ---")
    print("(columns: elapsed | gap | type | detail)\n")

    base_url = cfg["base_url"].rstrip("/")
    api_key = _resolve_env(cfg.get("api_key", "none"))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Hermes-Session-Key": "agent:onju:voice:test-stream",
        "X-Hermes-Session-Id": "onju-test-stream",
    }
    payload = {
        "model": cfg.get("model", "hermes-agent"),
        "messages": [{"role": "user", "content": content}],
        "max_tokens": cfg.get("max_tokens", 800),
        "user": "test-stream",
        "stream": True,
    }

    t0 = time.monotonic()
    prev = t0
    content_chars = 0
    tool_progress_events = 0
    event = None

    timeout = httpx.Timeout(cfg.get("timeout", 120.0), connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{base_url}/chat/completions",
                                 headers=headers, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    event = None
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()

                now = time.monotonic()
                elapsed = now - t0
                gap = now - prev
                prev = now
                gap_flag = " <<<" if gap > GAP_THRESHOLD else ""

                if data == "[DONE]":
                    print(f"[{elapsed:6.2f}s] +{gap:5.2f}s  [DONE]{gap_flag}")
                    break

                if event == "hermes.tool.progress":
                    tool_progress_events += 1
                    print(f"[{elapsed:6.2f}s] +{gap:5.2f}s  tool.progress  {data[:80]}{gap_flag}")
                    continue

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    print(f"[{elapsed:6.2f}s] +{gap:5.2f}s  unparsable  {data[:80]}{gap_flag}")
                    continue

                choices = chunk.get("choices") or [{}]
                delta = choices[0].get("delta", {})
                finish = choices[0].get("finish_reason")
                parts = []
                if delta.get("role"):
                    parts.append(f"role={delta['role']}")
                if delta.get("content"):
                    content_chars += len(delta["content"])
                    text = delta["content"].replace("\n", "\\n")
                    parts.append(f"[{len(delta['content'])}] {text}")
                if finish:
                    parts.append(f"finish_reason={finish}")
                label = " | ".join(parts) if parts else "(empty delta)"
                print(f"[{elapsed:6.2f}s] +{gap:5.2f}s  {label}{gap_flag}")

    total = time.monotonic() - t0
    print(f"\n[{total:6.2f}s] stream closed — {content_chars} content chars, "
          f"{tool_progress_events} tool.progress events\n")
    return tool_progress_events


async def sentence_pass(backend, prompt: str) -> None:
    """Run the prompt through the high-level stream → sentence_chunks path."""
    print("--- sentence chunks ---")
    t0 = time.monotonic()
    first_sentence_at: float | None = None
    sentences: list[str] = []
    async for sentence in sentence_chunks(backend.stream(prompt)):
        now = time.monotonic() - t0
        if first_sentence_at is None:
            first_sentence_at = now
        tag = "ACK " if len(sentences) == 0 else "    "
        print(f"[{now:6.2f}s] {tag}{sentence}")
        sentences.append(sentence)
    total = time.monotonic() - t0
    print(f"[{total:6.2f}s] done ({len(sentences)} sentences)\n")

    print("--- summary ---")
    if first_sentence_at is not None:
        print(f"first sentence flush: {first_sentence_at:.2f}s")
    print(f"full response       : {total:.2f}s")
    if first_sentence_at is not None and total > 0:
        head_ratio = first_sentence_at / total
        print(f"ack lead ratio      : {head_ratio:.0%} "
              f"(lower = more headroom for the ack to play while tools run)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test hermes-agent SSE streaming")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument("--raw-only", action="store_true",
                        help="Only run the raw SSE inspection, skip sentence pass")
    args = parser.parse_args()

    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pipeline", "config.yaml",
    )
    with open(cfg_path) as f:
        config = yaml.safe_load(f)
    config["conversation"]["backend"] = "hermes"
    hcfg = config["conversation"]["hermes"]

    voice_prompt = hcfg.get("voice_prompt")

    print(f"endpoint: {hcfg['base_url']}")
    print(f"model   : {hcfg.get('model', 'hermes-agent')}")
    if voice_prompt:
        print(f"voice   : {voice_prompt[:80]}...")
    print(f"prompt  : {args.prompt}\n")

    content = f"{voice_prompt}\n\n{args.prompt}" if voice_prompt else args.prompt
    tool_events = await raw_event_inspection(hcfg, content)

    if tool_events:
        print(f"** {tool_events} hermes.tool.progress events detected — Hermes "
              f"surfaces tool activity on this endpoint, and the backend filters "
              f"them out of the spoken text.\n")
    else:
        print("** No tool.progress events seen. Either the prompt didn't trigger "
              "tools or the toolset is restricted so no tool ran.\n")

    if not args.raw_only:
        backend = create_backend(config, device_id="test-stream")
        await sentence_pass(backend, args.prompt)


if __name__ == "__main__":
    asyncio.run(main())
