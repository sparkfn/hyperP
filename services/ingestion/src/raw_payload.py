"""Shared decoder for the ``raw_payload`` value stored on ``SourceRecord`` nodes.

``raw_payload`` is written as a JSON string — Neo4j cannot store a nested MAP as
a node property — so every reader that fetches ``sr.raw_payload`` from a Cypher
result must decode it back to a dict before use. Centralising the decode keeps
the str/dict/invalid branches in one place rather than re-implemented at every
call site (drain, propose, knows). This helper is pure: it returns ``None`` for
an undecodable/non-dict value, and the caller decides whether to warn (with the
row's source_record_pk) and/or mark the record link_failed.
"""

from __future__ import annotations

import json
import logging
from typing import cast

from src.models import JsonValue

logger = logging.getLogger(__name__)


def decode_raw_payload(raw: object) -> dict[str, JsonValue] | None:
    """Decode a ``SourceRecord.raw_payload`` value to a dict.

    Handles the JSON-string form (the normal Neo4j storage), an already-decoded
    dict (test mocks), and returns ``None`` for anything else or on parse
    failure (including ``RecursionError`` from a pathologically-nested string,
    which is not a ``ValueError`` subclass). Pure: callers own the warn/mark.
    """
    if isinstance(raw, dict):
        return cast("dict[str, JsonValue]", raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed: JsonValue = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None