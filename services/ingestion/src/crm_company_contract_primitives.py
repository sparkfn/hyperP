"""Private validation and digest primitives for immutable CRM company contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from functools import total_ordering

from src.crm_identity_associations import CrmCompanyBinding
from src.models import JsonValue
from src.standalone_crm_census_types import _integer, _text, _utc


@total_ordering
@dataclass(frozen=True)
class CrmSourceHeadOrderKey:
    """Chronological source-head order: instant, version, then source record key."""

    available_at: str
    source_record_version: int
    source_record_pk: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        _integer(self.source_record_version, "source_record_version", 1)
        _canonical_text(self.source_record_pk, "source_record_pk")

    def __lt__(self, other: CrmSourceHeadOrderKey) -> bool:
        return self._chronological_tuple() < other._chronological_tuple()

    def _chronological_tuple(self) -> tuple[datetime, int, str]:
        return (
            datetime.fromisoformat(self.available_at.replace("Z", "+00:00")),
            self.source_record_version,
            self.source_record_pk,
        )


def _positive_decimal(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.isdigit()
        or int(value) < 1
        or str(int(value)) != value
    ):
        raise ValueError(f"{field_name} must be canonical positive decimal")


def _canonical_text(value: str, field_name: str) -> None:
    normalized = _text(value, field_name)
    if normalized != value:
        raise ValueError(f"{field_name} must be canonical")


def _matching_binding(
    bindings: tuple[CrmCompanyBinding, ...], company_id: str
) -> CrmCompanyBinding | None:
    return next((binding for binding in bindings if binding.company_id == company_id), None)


def _digest(namespace: str, payload: list[JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    hashed = namespace.encode("utf-8") + b"\x00" + encoded.encode("utf-8")
    return hashlib.sha256(hashed).hexdigest()
