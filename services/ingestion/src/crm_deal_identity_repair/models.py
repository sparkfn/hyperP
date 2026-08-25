"""Typed, immutable values for CRM-deal identity repair inventory."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, cast

from src.models import JsonValue

RepairPartition = Literal["ownership_repair", "projection_cleanup", "negative_control"]


@dataclass(frozen=True, init=False)
class RepairInventoryItem:
    """One frozen read-only inventory row for a logical CRM deal version."""

    source_system: str
    source_record_id: str
    source_record_pk: str
    deal_id: str
    partition: RepairPartition
    repair_conditions: tuple[RepairPartition, ...]
    graph_fingerprint: str
    stored_payload_fingerprint: str
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        source_system: str,
        source_record_id: str,
        source_record_pk: str,
        deal_id: str,
        partition: RepairPartition,
        repair_conditions: tuple[RepairPartition, ...] | None = None,
        graph_fingerprint: str,
        stored_payload_fingerprint: str,
        payload: dict[str, JsonValue],
    ) -> None:
        object.__setattr__(self, "source_system", source_system)
        object.__setattr__(self, "source_record_id", source_record_id)
        object.__setattr__(self, "source_record_pk", source_record_pk)
        object.__setattr__(self, "deal_id", deal_id)
        object.__setattr__(self, "partition", partition)
        object.__setattr__(
            self,
            "repair_conditions",
            repair_conditions if repair_conditions is not None else (partition,),
        )
        object.__setattr__(self, "graph_fingerprint", graph_fingerprint)
        object.__setattr__(self, "stored_payload_fingerprint", stored_payload_fingerprint)
        object.__setattr__(self, "_payload_json", _encode_json_object(payload))
        self._validate()

    def _validate(self) -> None:
        for label, value in (
            ("source_system", self.source_system),
            ("source_record_id", self.source_record_id),
            ("source_record_pk", self.source_record_pk),
            ("deal_id", self.deal_id),
        ):
            if not value:
                raise ValueError(f"repair inventory {label} must be non-empty")
        if self.partition not in {
            "ownership_repair",
            "projection_cleanup",
            "negative_control",
        }:
            raise ValueError("repair inventory partition is invalid")
        if not self.repair_conditions or len(self.repair_conditions) != len(
            set(self.repair_conditions)
        ):
            raise ValueError("repair inventory conditions must be non-empty and unique")
        if any(
            condition not in {"ownership_repair", "projection_cleanup", "negative_control"}
            for condition in self.repair_conditions
        ):
            raise ValueError("repair inventory condition is invalid")
        if "negative_control" in self.repair_conditions and len(self.repair_conditions) != 1:
            raise ValueError("negative control cannot overlap a repair condition")
        if self.partition not in self.repair_conditions:
            raise ValueError("repair inventory primary partition must be a condition")
        _validate_sha256_digest(self.graph_fingerprint, "graph fingerprint")
        _validate_sha256_digest(self.stored_payload_fingerprint, "stored payload fingerprint")

    @property
    def payload(self) -> dict[str, JsonValue]:
        """Return a fresh mutable copy backed by immutable canonical JSON."""
        return _decode_json_object(self._payload_json)

    @property
    def inventory_key(self) -> str:
        """Stable identity used for deterministic ordering and duplicate rejection."""
        return "|".join((self.source_system, self.source_record_id, self.source_record_pk))

    def to_dict(self) -> dict[str, JsonValue]:
        """Return a canonical-JSON-safe copy of this immutable inventory row."""
        conditions: list[JsonValue] = [condition for condition in self.repair_conditions]
        return {
            "source_system": self.source_system,
            "source_record_id": self.source_record_id,
            "source_record_pk": self.source_record_pk,
            "deal_id": self.deal_id,
            "partition": self.partition,
            "repair_conditions": conditions,
            "graph_fingerprint": self.graph_fingerprint,
            "stored_payload_fingerprint": self.stored_payload_fingerprint,
            "payload": self.payload,
        }


def _encode_json_object(value: dict[str, JsonValue]) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("repair inventory payload must contain finite JSON values") from exc


def _decode_json_object(encoded: str) -> dict[str, JsonValue]:
    decoded = cast(JsonValue, json.loads(encoded))
    if not isinstance(decoded, dict):
        raise RuntimeError("stored repair inventory payload is not a JSON object")
    return decoded


def _validate_sha256_digest(value: str, label: str) -> None:
    prefix, separator, hexadecimal = value.partition(":")
    if (
        prefix != "sha256"
        or separator != ":"
        or len(hexadecimal) != 64
        or any(character not in "0123456789abcdef" for character in hexadecimal)
    ):
        raise ValueError(f"repair inventory {label} must be a lowercase sha256 digest")
