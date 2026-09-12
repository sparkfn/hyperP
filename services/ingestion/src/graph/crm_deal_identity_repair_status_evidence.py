"""Incremental canonical evidence encoders for CRM-deal repair status."""

from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import ExitStack
from hashlib import sha256

from neo4j import ManagedTransaction

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.bounded import CanonicalByteSorter
from src.graph.crm_deal_identity_repair_boundary_evidence import (
    canonical_boundary_evidence,
    record_json_dict,
)
from src.models import JsonValue


class CanonicalObjectDigest:
    """Incrementally encode exactly the canonical bytes authenticated by object_digest."""

    def __init__(self, domain: bytes) -> None:
        self._digest = sha256()
        self._digest.update(domain)
        self._digest.update(b"{")
        self._last_key: str | None = None
        self._array_open = False
        self._array_first = True

    def value(self, key: str, value: JsonValue) -> None:
        self._prefix(key)
        self._digest.update(canonical_json_line(value)[:-1])

    def array(self, key: str, values: Iterable[bytes]) -> None:
        self.begin_array(key)
        for value in values:
            self.array_value(value)
        self.end_array()

    def begin_array(self, key: str) -> None:
        self._prefix(key)
        self._digest.update(b"[")
        self._array_open = True
        self._array_first = True

    def array_value(self, value: bytes) -> None:
        if not self._array_open:
            raise RuntimeError("canonical boundary array is not open")
        if not value.endswith(b"\n"):
            raise RuntimeError("bounded boundary evidence is not canonical JSON")
        if not self._array_first:
            self._digest.update(b",")
        self._digest.update(value[:-1])
        self._array_first = False

    def end_array(self) -> None:
        if not self._array_open:
            raise RuntimeError("canonical boundary array is not open")
        self._digest.update(b"]")
        self._array_open = False

    def finish(self) -> str:
        if self._array_open:
            raise RuntimeError("canonical boundary array was not closed")
        self._digest.update(b"}\n")
        return "sha256:" + self._digest.hexdigest()

    def _prefix(self, key: str) -> None:
        if self._last_key is not None:
            if key <= self._last_key:
                raise RuntimeError("canonical boundary object fields are not ordered")
            self._digest.update(b",")
        self._digest.update(canonical_json_line(key)[:-1])
        self._digest.update(b":")
        self._last_key = key


def spool_evidence(
    stack: ExitStack,
    tx: ManagedTransaction,
    query: str,
    **parameters: str,
) -> tuple[CanonicalByteSorter, int]:
    """Fully consume, canonicalize, and disk-sort one unordered evidence family."""
    sorter = stack.enter_context(CanonicalByteSorter())
    result = tx.run(query, **parameters)
    count = 0
    for record in result:
        value = canonical_boundary_evidence(record_json_dict(record))
        if not isinstance(value, dict):
            raise RuntimeError("repair boundary evidence rows must be JSON objects")
        sorter.add(canonical_json_bytes({"value": value}), canonical_json_bytes(value))
        count += 1
    result.consume()
    return sorter, count


def canonical_json_line(value: JsonValue) -> bytes:
    """Serialize any validated JSON value with the repository's canonical settings."""
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("repair boundary evidence contains non-finite JSON") from exc
    return (encoded + "\n").encode("utf-8")
