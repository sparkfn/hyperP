"""Connector for SG Rental Flats dump records."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import build_envelope
from src.connectors.sggov.dump import CopyRow, iter_copy_rows
from src.models import JsonValue

_COPY_BATCH_SIZE = 1000


def _str_value(row: CopyRow, key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bool_value(row: CopyRow, key: str) -> bool:
    return _str_value(row, key).lower() in {"t", "true", "1", "yes"}


def _iso_datetime(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized = f"{normalized}:00"
    datetime.fromisoformat(normalized)
    return normalized


def build_rental_flat_envelope(
    *,
    flat_id: str,
    town_id: str,
    block_no: str,
    street_name: str,
    postal_code: str,
    flat_type: str,
    town_name: str,
    town_map_id: str,
    town_map_zone: str,
    is_active: bool,
    observed_at: JsonValue,
    raw_payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    normalized_observed_at = _iso_datetime(observed_at) or datetime.utcnow().isoformat()
    return build_envelope(
        source_record_id=f"rental_flat:{flat_id}",
        observed_at=normalized_observed_at,
        identifiers=[],
        record_type="rental_flat",
        attributes={
            "country_code": "SG",
            "postal_code": postal_code,
            "block_no": block_no,
            "street_name": street_name,
            "flat_type": flat_type,
            "town_id": town_id,
            "town_name": town_name,
            "town_map_id": town_map_id,
            "town_map_zone": town_map_zone,
            "is_active": is_active,
        },
        raw_payload=raw_payload,
    )


class SGGovernmentRentalFlatsConnector(SourceConnector):
    """Read rental-flat address inventory from an SG government SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "sgrentalflats"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        flats = iter_copy_rows(self._dump_path, "flats")
        for flat_batch in _copy_row_batches(flats):
            town_ids = {_str_value(flat, "town_id") for flat in flat_batch}
            towns = {
                town_id: town
                for town in iter_copy_rows(self._dump_path, "towns")
                if (town_id := _str_value(town, "id")) in town_ids
            }
            for flat in flat_batch:
                yield self._build_flat(flat, towns)

    def _build_flat(
        self,
        flat: CopyRow,
        towns: dict[str, CopyRow],
    ) -> dict[str, JsonValue]:
        flat_id = _str_value(flat, "id")
        town_id = _str_value(flat, "town_id")
        town = towns.get(town_id, {})
        raw_payload: dict[str, JsonValue] = {"flat": flat, "town": town}
        return build_rental_flat_envelope(
            flat_id=flat_id,
            town_id=town_id,
            block_no=_str_value(flat, "block_no"),
            street_name=_str_value(flat, "street_name"),
            postal_code=_str_value(flat, "postal_code"),
            flat_type=_str_value(flat, "flat_type"),
            town_name=_str_value(town, "name"),
            town_map_id=_str_value(town, "map_id"),
            town_map_zone=_str_value(town, "map_zone"),
            is_active=_bool_value(flat, "is_active"),
            observed_at=flat.get("last_seen_at"),
            raw_payload=raw_payload,
        )


def _copy_row_batches(rows: Iterator[CopyRow]) -> Iterator[list[CopyRow]]:
    batch: list[CopyRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) == _COPY_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch
