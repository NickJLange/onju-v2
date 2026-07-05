import re
from typing import AsyncIterator

from pipeline.conversation.base import ConversationBackend
from pipeline.conversation.conversational import ConversationalBackend
from pipeline.conversation.hermes import HermesBackend

# Primary: punctuation followed by whitespace (safe, standard).
_SENTENCE_END = re.compile(r"[.!?\n]+\s+")
# Fallback: punctuation with no space, but only when preceded by a lowercase
# letter and followed by uppercase.  Catches agent chunk-boundary joins
# ("now.The") without breaking abbreviations like "U.S." (uppercase before dot).
_SENTENCE_END_NOSPACE = re.compile(r"(?<=[a-z])[.!?]+(?=[A-Z])")


async def sentence_chunks(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Buffer text deltas and yield one sentence at a time, plus any trailing
    fragment when the stream ends."""
    buffer = ""
    async for delta in deltas:
        buffer += delta
        while True:
            m1 = _SENTENCE_END.search(buffer)
            m2 = _SENTENCE_END_NOSPACE.search(buffer)
            matches = [m for m in (m1, m2) if m]
            if not matches:
                break
            m = min(matches, key=lambda x: x.start())
            sentence = buffer[: m.end()].strip()
            buffer = buffer[m.end():]
            if sentence:
                yield sentence
    tail = buffer.strip()
    if tail:
        yield tail


def _resolve_hermes_cfg(conv_cfg: dict, device_id: str) -> dict:
    """Return the hermes profile config for a given device_id.

    Resolution order:
      1. hermes_profiles + device_routes: exact hostname match → "default" → first profile
      2. Fallback: legacy single ``hermes:`` key (backwards-compat)
    """
    profiles = conv_cfg.get("hermes_profiles", {})
    if profiles:
        routes = conv_cfg.get("device_routes") or {}
        name = routes.get(device_id) or routes.get("default") or next(iter(profiles))
        if name not in profiles:
            raise ValueError(
                f"device_routes maps '{device_id}' to profile '{name}' "
                f"but that profile is not defined in hermes_profiles"
            )
        return profiles[name]
    # backwards-compat: single hermes block
    hermes_cfg = conv_cfg.get("hermes")
    if hermes_cfg is None:
        raise ValueError(
            "conversation backend is 'hermes' but config has neither "
            "'hermes_profiles' nor a 'hermes' block"
        )
    return hermes_cfg


def create_backend(config: dict, device_id: str) -> ConversationBackend:
    """Create a conversation backend based on config."""
    conv_cfg = config["conversation"]
    backend = conv_cfg.get("backend", "conversational")

    if backend == "conversational":
        return ConversationalBackend(conv_cfg["conversational"], device_id)
    elif backend == "hermes":
        return HermesBackend(_resolve_hermes_cfg(conv_cfg, device_id), device_id)
    else:
        raise ValueError(f"Unknown conversation backend: {backend}")
