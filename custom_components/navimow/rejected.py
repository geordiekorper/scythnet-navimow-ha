"""Input the integration receives but does not apply.

Late, implausible or unparsable messages and fields the decoders do not know
must not change the mower's entities, but they are evidence: a late pose is
still a pose, and an unknown field is how a new vendor feature shows up. Each
such item is recorded on the mower's ``rejected_input`` diagnostic sensor
(state: a running count; attributes: the latest item), so the recorder keeps
every one of them and they can be read back through history like any other
state.
"""
from __future__ import annotations

import json
from typing import Any

# Why an item was not applied. `stale`: older than the newest applied item of
# the same kind. `implausible_time`: stamped before 2020 or in the future.
# `placeholder`: an all-zero pose a standing mower sends in place of a
# position. `unknown_type` / `unknown_field`: vendor data no decoder knows.
# `unknown_channel`: a whole MQTT channel nothing decodes. `unparsable`: not
# JSON, or not the shape the channel carries.
REASONS = (
    "stale", "implausible_time", "placeholder", "unknown_type",
    "unknown_field", "unknown_channel", "unparsable",
)

# The recorder drops a state whose attributes, as UTF-8 JSON, exceed 16 KB.
# A payload is cut to this many bytes in that form (quotes and backslashes
# escaped, other characters as UTF-8), which keeps the whole record well
# below the limit.
PAYLOAD_LIMIT = 8000


def _json_size(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


def _cut(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose JSON-escaped UTF-8 size fits ``limit``."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_size(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def rejection_record(
    channel: str,
    topic: str | None,
    reason: str,
    payload: Any,
    received_at: str,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    """The attributes describing one rejected item.

    ``payload`` is kept as text: a string as received, anything else as
    JSON. ``reasons`` lists every reason when there is more than one;
    ``reason`` is the one that decided.
    """
    if reason not in REASONS:
        raise ValueError(f"unknown rejection reason: {reason}")
    text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"), default=str)
    truncated = _json_size(text) > PAYLOAD_LIMIT
    return {
        "channel": channel,
        "topic": topic,
        "reason": reason,
        "reasons": list(reasons) if reasons else [reason],
        "received_at": received_at,
        "payload": _cut(text, PAYLOAD_LIMIT) if truncated else text,
        "truncated": truncated,
    }
