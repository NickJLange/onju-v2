import json
import logging
import os
import re
import uuid
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

log = logging.getLogger(__name__)


def _resolve_env(value: str) -> str:
    def _sub(m: re.Match) -> str:
        env_val = os.environ.get(m.group(1))
        if env_val is None:
            log.warning("api_key references $%s which is not set — auth will fail", m.group(1))
            return ""
        return env_val
    return re.sub(r"\$\{(\w+)\}", _sub, value)


class HermesBackend:
    """Delegates conversation memory and tool execution to a remote
    hermes-agent API server over its OpenAI-compatible
    ``/v1/chat/completions`` endpoint.

    Only the latest user message is sent — Hermes tracks session history
    server-side, scoped per speaker via the ``X-Hermes-Session-Key`` header,
    and runs its own tool loop. The voice channel should reach a *restricted*
    toolset (no host terminal / file-write / code execution); see
    ``docs/hermes-secure-setup.md`` for how to lock that down on the Hermes
    side via ``platform_toolsets.api_server``.

    Session model:
      * ``session_key`` is a stable long-term-memory scope for this device, so
        the agent keeps knowing this speaker across calls. It does NOT rotate.
      * ``session_id`` is the transcript scope; it rotates on ``reset()`` to
        start a fresh conversation while preserving long-term memory.
    """

    def __init__(self, cfg: dict, device_id: str):
        self.cfg = cfg
        self.device_id = device_id
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = _resolve_env(cfg.get("api_key", "none"))

        prefix = cfg.get("session_key_prefix", "agent:onju:voice")
        self.session_key = f"{prefix}:{device_id}"
        self.session_id = self._new_session_id()

        # OpenAI SDK is fine for the non-streaming send() (single JSON response,
        # no custom SSE events). stream() uses raw httpx — see below.
        self.client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)

    def _new_session_id(self) -> str:
        return f"onju-{self.device_id}-{uuid.uuid4().hex[:8]}"

    def _headers(self) -> dict:
        return {
            "X-Hermes-Session-Key": self.session_key,
            "X-Hermes-Session-Id": self.session_id,
        }

    def _build_content(self, user_text: str, extra_context: str | None = None) -> str:
        parts = []
        if voice_prompt := self.cfg.get("voice_prompt"):
            parts.append(voice_prompt)
        if extra_context:
            parts.append(extra_context)
        parts.append(user_text)
        return "\n\n".join(parts)

    def _build_kwargs(self, user_text: str, extra_context: str | None = None) -> dict:
        return dict(
            model=self.cfg.get("model", "hermes-agent"),
            messages=[{"role": "user", "content": self._build_content(user_text, extra_context)}],
            max_tokens=self.cfg.get("max_tokens", 300),
            user=self.device_id,
        )

    async def send(self, user_text: str, extra_context: str | None = None) -> str:
        response = await self.client.chat.completions.create(
            extra_headers=self._headers(),
            **self._build_kwargs(user_text, extra_context),
        )
        text = response.choices[0].message.content or ""
        log.debug(f"[{self.device_id}] hermes: {text}")
        return text

    async def stream(self, user_text: str, extra_context: str | None = None) -> AsyncIterator[str]:
        """Consume the Hermes SSE stream directly.

        Hermes interleaves standard ``chat.completion.chunk`` data events with a
        custom ``event: hermes.tool.progress`` event. The stock OpenAI SDK would
        try to parse the progress event's ``data:`` as a chat chunk; reading the
        raw SSE lets us route it cleanly so it never reaches the spoken text.
        """
        payload = {**self._build_kwargs(user_text, extra_context), "stream": True}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self._headers(),
        }
        # Agentic turns can take 5-60s+ while tools run; keep a generous read
        # timeout but fail fast on connect.
        timeout = httpx.Timeout(self.cfg.get("timeout", 120.0), connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions", headers=headers, json=payload
            ) as resp:
                resp.raise_for_status()
                async for delta in self._iter_sse_content(resp.aiter_lines()):
                    yield delta

    async def _iter_sse_content(self, lines: AsyncIterator[str]) -> AsyncIterator[str]:
        """Yield assistant content deltas from a Hermes SSE line stream,
        skipping ``hermes.tool.progress`` events and the ``[DONE]`` terminator.
        Factored out so it can be unit-tested without a live server."""
        event: str | None = None
        async for line in lines:
            if not line:
                event = None  # blank line terminates the current SSE event
                continue
            if line.startswith(":"):
                continue  # comment / keepalive
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            if event == "hermes.tool.progress":
                self._on_tool_progress(data)
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices")
            if not choices:
                continue
            content = choices[0].get("delta", {}).get("content")
            if content:
                yield content

    def _on_tool_progress(self, data: str) -> None:
        """Hook for tool-start progress events. Logged for now; this is the
        seam where LED activity could be driven while the agent runs tools."""
        log.debug(f"[{self.device_id}] tool progress: {data}")

    def commit(self, text: str) -> None:
        pass  # Hermes owns history server-side

    def reset(self) -> None:
        # Fresh transcript; long-term memory scope (session_key) stays stable.
        self.session_id = self._new_session_id()

    def get_messages(self) -> list[dict]:
        return []  # history lives on the Hermes server

    def set_messages(self, messages: list[dict]) -> None:
        pass  # no-op — Hermes owns the history
