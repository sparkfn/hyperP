"""Shared decoder and scrubber for ``raw_payload`` stored on ``SourceRecord`` nodes.

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
import re
from typing import cast

from src.models import JsonValue, SourceRecordEnvelope
from src.normalizers.nric import mask_nric, normalize_nric

logger = logging.getLogger(__name__)

_NRIC_REGEX = re.compile(r"(?i)[STFG]\d{7}[A-Z]")
_NRIC_KEY_NAMES = frozenset({"nric", "customer_nric", "ic_number", "identification_number"})
MIN_COLLECTED_NRIC_TOKEN_LEN = 8


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


def _collect_payload_nric_tokens(obj: object) -> set[str]:
    """Collect raw and canonical NRIC strings under known NRIC keys in a JSON structure."""
    tokens: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in _NRIC_KEY_NAMES and isinstance(v, str):
                    s = v.strip()
                    tokens.add(s)
                    canonical, _ = normalize_nric(s)
                    if canonical:
                        tokens.add(canonical)
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return tokens


def _scrub_nric_value(
    val: JsonValue,
    collected_tokens: frozenset[str],
) -> JsonValue:
    if isinstance(val, str):
        result = val
        for tok in collected_tokens:
            if not tok or len(tok) < MIN_COLLECTED_NRIC_TOKEN_LEN:
                continue
            # Case-insensitive replacement of the exact token with masked form
            pattern = re.compile(re.escape(tok), re.IGNORECASE)
            result = pattern.sub(lambda m: mask_nric(m.group(0)), result)
        # Regex pass for free-text NRICs
        return _NRIC_REGEX.sub(lambda m: mask_nric(m.group(0)), result)
    if isinstance(val, list):
        return cast(JsonValue, [_scrub_nric_value(item, collected_tokens) for item in val])
    if isinstance(val, dict):
        return cast(
            JsonValue,
            {k: _scrub_nric_value(v, collected_tokens) for k, v in val.items()},
        )
    return val


def scrub_nric_strings(
    val: JsonValue,
    collected_tokens: frozenset[str] = frozenset(),
) -> JsonValue:
    """Recursively mask NRIC values in any JSON data structure.

    Pass 1: replace any known collected NRIC tokens (case-insensitive), including
            tokens under named NRIC keys within ``val``.
    Pass 2: replace any remaining regex matches for ``[STFG]\\d{7}[A-Z]``.
    """
    tokens = collected_tokens | _collect_payload_nric_tokens(val)
    return _scrub_nric_value(val, tokens)


def _collect_nric_tokens(envelope: SourceRecordEnvelope) -> set[str]:
    """Collect known raw/canonical NRIC strings from envelope and payload keys."""
    tokens = _collect_payload_nric_tokens(envelope.raw_payload)
    for ident in envelope.identifiers:
        if ident.type.lower().strip() == "nric":
            tokens.add(ident.value.strip())
            canonical, _ = normalize_nric(ident.value)
            if canonical:
                tokens.add(canonical)
    return tokens


def serialize_raw_payload(envelope: SourceRecordEnvelope) -> str:
    """Serialize envelope.raw_payload to a JSON string with all NRIC strings masked.

    Pass 1: Collected envelope NRIC identifiers (raw + canonical) and values under
            named NRIC keys ({nric, customer_nric, ic_number, identification_number}).
    Pass 2: Free-text regex [STFG]\\d{7}[A-Z] over every string leaf.
    """
    collected = frozenset(_collect_nric_tokens(envelope))
    scrubbed = scrub_nric_strings(
        cast(JsonValue, envelope.raw_payload),
        collected_tokens=collected,
    )
    return json.dumps(scrubbed, default=str)
