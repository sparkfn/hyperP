"""Immutable ASSERTS edges derived from normalized SourceRecord payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from neo4j import ManagedTransaction

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.queries.identity_assertions import (
    ASSERT_ADDRESSES,
    ASSERT_FACTS,
    ASSERT_IDENTIFIERS,
    AUDIT_ASSERTION_PAGE,
    BACKFILL_ASSERTION_PAGE,
    MERGE_PROFILE_FACTS_BATCH,
)
from src.graph.queries.persons import UPSERT_ADDRESSES_BATCH, UPSERT_IDENTIFIERS_BATCH
from src.identifier_scopes import identifier_scope, source_instance_for_identifier
from src.identity_registry import FACT_ASSERTION_ATTRIBUTES
from src.models import JsonValue, QualityFlag
from src.pipeline_normalization import is_usable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssertionRows:
    """Deduplicated target upsert and assertion rows for one SourceRecord."""

    identifiers: list[dict[str, JsonValue]]
    addresses: list[dict[str, JsonValue]]
    facts: list[dict[str, JsonValue]]
    malformed: int = 0


@dataclass(frozen=True)
class AssertionAuditMismatch:
    """Expected and stored ASSERTS counts for a SourceRecord."""

    source_record_pk: str
    expected_identifiers: int
    actual_identifiers: int
    expected_addresses: int
    actual_addresses: int
    expected_facts: int
    actual_facts: int
    malformed_entries: int = 0


class _MalformedEntryError(ValueError):
    """A payload entry lacks the shape the row builder needs."""


RowBuilder = Callable[[str, Mapping[str, JsonValue]], dict[str, JsonValue] | None]


def _quality(raw: Mapping[str, JsonValue]) -> QualityFlag:
    value = raw.get("quality_flag")
    if isinstance(value, str):
        try:
            return QualityFlag(value)
        except ValueError:
            pass
    raise _MalformedEntryError


def _collect(
    source_record_pk: str,
    entries: JsonValue | None,
    build: RowBuilder,
) -> tuple[list[dict[str, JsonValue]], int]:
    """Build deduplicated rows; count malformed entries and present non-list containers."""
    if entries is None:
        return [], 0
    if not isinstance(entries, list):
        return [], 1
    rows: dict[str, dict[str, JsonValue]] = {}
    malformed = 0
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                raise _MalformedEntryError
            row = build(source_record_pk, entry)
        except _MalformedEntryError:
            malformed += 1
            continue
        if row is not None:
            rows[str(row["assertion_id"])] = row
    return [rows[key] for key in sorted(rows)], malformed


def assertion_rows(
    source_record_pk: str,
    normalized_payload: Mapping[str, JsonValue],
) -> AssertionRows:
    """Build stable rows for usable identifiers, addresses, and selected facts.

    ``malformed`` counts entries that are not objects or lack the fields a row needs, and any
    present container that is not a list; entries that are well formed but unusable (low quality,
    other attributes) are intentionally absent. A missing or empty ``addresses`` falls back to
    the legacy single ``address``.
    """
    addresses = normalized_payload.get("addresses")
    legacy_address = normalized_payload.get("address")
    if addresses in (None, []) and legacy_address is not None:
        addresses = [legacy_address]
    identifier_rows, bad_identifiers = _collect(
        source_record_pk, normalized_payload.get("identifiers"), _identifier_row
    )
    address_rows, bad_addresses = _collect(source_record_pk, addresses, _address_row)
    fact_rows, bad_facts = _collect(
        source_record_pk, normalized_payload.get("attributes"), _fact_row
    )
    return AssertionRows(
        identifiers=identifier_rows,
        addresses=address_rows,
        facts=fact_rows,
        malformed=bad_identifiers + bad_addresses + bad_facts,
    )


def _identifier_row(
    source_record_pk: str,
    raw: Mapping[str, JsonValue],
) -> dict[str, JsonValue] | None:
    identifier_type = _string(raw, "identifier_type")
    normalized_value = _string(raw, "normalized_value")
    if identifier_type is None or normalized_value is None:
        raise _MalformedEntryError
    if not is_usable(_quality(raw)):
        return None
    source_instance_id = _optional_string(raw.get("source_instance_id"))
    scope = identifier_scope(identifier_type, source_instance_id)
    target_id = f"{identifier_type}|{scope}|{normalized_value}"
    field_path = f"identifiers.{identifier_type}"
    return {
        "source_record_pk": source_record_pk,
        "identifier_type": identifier_type,
        "identifier_scope": scope,
        "normalized_value": normalized_value,
        "source_instance_id": source_instance_for_identifier(identifier_type, source_instance_id),
        "target_id": target_id,
        "field_path": field_path,
        "assertion_id": _assertion_id(source_record_pk, field_path, target_id),
    }


def _address_row(
    source_record_pk: str,
    raw: Mapping[str, JsonValue],
) -> dict[str, JsonValue] | None:
    if not is_usable(_quality(raw)):
        return None
    country_code = _string(raw, "country_code") or "SG"
    postal_code = _string(raw, "postal_code") or ""
    street_name = _string(raw, "street_name") or ""
    street_number = _string(raw, "street_number") or ""
    unit_number = _string(raw, "unit_number") or ""
    target_id = "|".join((country_code, postal_code, street_name, street_number, unit_number))
    field_path = "addresses"
    return {
        "source_record_pk": source_record_pk,
        "country_code": country_code,
        "postal_code": postal_code,
        "street_name": street_name,
        "street_number": street_number,
        "unit_number": unit_number,
        "building_name": _optional_string(raw.get("building_name")),
        "city": _string(raw, "city") or "",
        "state_province": _optional_string(raw.get("state_province")),
        "normalized_full": _string(raw, "normalized_full") or "",
        "target_id": target_id,
        "field_path": field_path,
        "assertion_id": _assertion_id(source_record_pk, field_path, target_id),
    }


def _fact_row(
    source_record_pk: str,
    raw: Mapping[str, JsonValue],
) -> dict[str, JsonValue] | None:
    attribute_name = _string(raw, "attribute_name")
    attribute_value = _string(raw, "attribute_value")
    if attribute_name is None or attribute_value is None:
        raise _MalformedEntryError
    if attribute_name not in FACT_ASSERTION_ATTRIBUTES:
        return None
    target_id = f"{attribute_name}|{attribute_value}"
    field_path = f"attributes.{attribute_name}"
    return {
        "source_record_pk": source_record_pk,
        "attribute_name": attribute_name,
        "attribute_value": attribute_value,
        "target_id": target_id,
        "field_path": field_path,
        "assertion_id": _assertion_id(source_record_pk, field_path, target_id),
    }


def _assertion_id(source_record_pk: str, field_path: str, target_id: str) -> str:
    identity = f"{source_record_pk}|{field_path}|{target_id}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def _string(row: Mapping[str, JsonValue], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) else None


def _optional_string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def write_assertions(
    tx: ManagedTransaction,
    source_record_pk: str,
    normalized_payload: Mapping[str, JsonValue],
) -> None:
    """Upsert assertion targets and immutably MERGE edges from one SourceRecord."""
    rows = assertion_rows(source_record_pk, normalized_payload)
    if rows.identifiers:
        tx.run(UPSERT_IDENTIFIERS_BATCH, identifiers=rows.identifiers)
    if rows.addresses:
        tx.run(UPSERT_ADDRESSES_BATCH, addresses=rows.addresses)
    if rows.facts:
        tx.run(MERGE_PROFILE_FACTS_BATCH, facts=rows.facts)
    if rows.identifiers:
        tx.run(ASSERT_IDENTIFIERS, assertions=rows.identifiers)
    if rows.addresses:
        tx.run(ASSERT_ADDRESSES, assertions=rows.addresses)
    if rows.facts:
        tx.run(ASSERT_FACTS, assertions=rows.facts)


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("normalized payload contains a non-string key")
            result[key] = _json_value(item)
        return result
    raise ValueError("normalized payload contains a non-JSON value")


def _parse_normalized_payload(value: object) -> Mapping[str, JsonValue] | None:
    raw = value
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(raw, Mapping):
        return None
    try:
        parsed = _json_value(dict(raw))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _fallback_payload(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, list):
        return {"identifiers": []}
    parsed = _parse_normalized_payload({"identifiers": value})
    return parsed if parsed is not None else {"identifiers": []}


def _payload_or_fallback(record: Mapping[str, object]) -> Mapping[str, JsonValue]:
    payload = _parse_normalized_payload(record.get("normalized_payload"))
    if payload is not None and isinstance(payload.get("identifiers"), list):
        return payload
    return _fallback_payload(record.get("fallback_identifiers"))


def _read_page(
    tx: ManagedTransaction,
    query: str,
    after_source_record_pk: str | None,
    batch_size: int,
) -> list[dict[str, object]]:
    return [
        {
            "source_record_pk": record["source_record_pk"],
            "normalized_payload": record["normalized_payload"],
            "fallback_identifiers": record.get("fallback_identifiers"),
            "identifier_count": record.get("identifier_count"),
            "address_count": record.get("address_count"),
            "fact_count": record.get("fact_count"),
        }
        for record in tx.run(
            query,
            after_source_record_pk=after_source_record_pk,
            batch_size=batch_size,
        )
    ]


def _fetch_page(
    client: Neo4jClient,
    query: str,
    after_source_record_pk: str | None,
    batch_size: int,
) -> list[dict[str, object]]:
    def _work(tx: ManagedTransaction) -> list[dict[str, object]]:
        return _read_page(tx, query, after_source_record_pk, batch_size)

    return client.execute_read(_work)


def _assert_record(
    client: Neo4jClient,
    source_record_pk: str,
    payload: Mapping[str, JsonValue],
) -> None:
    def _work(tx: ManagedTransaction) -> None:
        write_assertions(tx, source_record_pk, payload)

    client.execute_write(_work)


def backfill_assertions(client: Neo4jClient, batch_size: int = 500) -> int:
    """Backfill ASSERTS for active, pending, parked, and legacy-current records."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    after_source_record_pk: str | None = None
    processed = 0
    while True:
        page = _fetch_page(client, BACKFILL_ASSERTION_PAGE, after_source_record_pk, batch_size)
        if not page:
            return processed
        for record in page:
            source_record_pk = record.get("source_record_pk")
            if not isinstance(source_record_pk, str):
                continue
            payload = _payload_or_fallback(record)
            rows = assertion_rows(source_record_pk, payload)
            if rows.malformed > 0:
                logger.warning(
                    "SourceRecord %s has %d malformed assertion entries in payload",
                    source_record_pk,
                    rows.malformed,
                )
            _assert_record(client, source_record_pk, payload)
            processed += 1
        last_pk = page[-1].get("source_record_pk")
        if not isinstance(last_pk, str):
            return processed
        after_source_record_pk = last_pk


def _expected_rows(record: Mapping[str, object], source_record_pk: str) -> AssertionRows:
    payload = _payload_or_fallback(record)
    return assertion_rows(source_record_pk, payload)


def _count(record: Mapping[str, object], key: str) -> int:
    value = record.get(key)
    return value if isinstance(value, int) else 0


def audit_assertions(client: Neo4jClient, batch_size: int = 500) -> list[AssertionAuditMismatch]:
    """Return active SourceRecords whose expected assertion counts do not match."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    mismatches: list[AssertionAuditMismatch] = []
    after_source_record_pk: str | None = None
    while True:
        page = _fetch_page(client, AUDIT_ASSERTION_PAGE, after_source_record_pk, batch_size)
        if not page:
            return mismatches
        for record in page:
            source_record_pk = record.get("source_record_pk")
            if not isinstance(source_record_pk, str):
                continue
            expected = _expected_rows(record, source_record_pk)
            actual_identifiers = _count(record, "identifier_count")
            actual_addresses = _count(record, "address_count")
            actual_facts = _count(record, "fact_count")
            if (
                len(expected.identifiers) != actual_identifiers
                or len(expected.addresses) != actual_addresses
                or len(expected.facts) != actual_facts
                or expected.malformed > 0
            ):
                mismatches.append(
                    AssertionAuditMismatch(
                        source_record_pk=source_record_pk,
                        expected_identifiers=len(expected.identifiers),
                        actual_identifiers=actual_identifiers,
                        expected_addresses=len(expected.addresses),
                        actual_addresses=actual_addresses,
                        expected_facts=len(expected.facts),
                        actual_facts=actual_facts,
                        malformed_entries=expected.malformed,
                    )
                )
        last_pk = page[-1].get("source_record_pk")
        if not isinstance(last_pk, str):
            return mismatches
        after_source_record_pk = last_pk


def main() -> int:
    """Run the guarded assertion backfill or the read-only count audit."""
    parser = argparse.ArgumentParser(description="Manage SourceRecord identity assertions")
    parser.add_argument("command", choices=("backfill", "audit"))
    args = parser.parse_args()
    settings = get_settings()
    if args.command == "backfill" and settings.identity_model != "v2_shadow":
        print("backfill requires IDENTITY_MODEL=v2_shadow", file=sys.stderr)
        return 2
    client = Neo4jClient(settings)
    try:
        if args.command == "backfill":
            print(f"Processed {backfill_assertions(client)} SourceRecords")
        else:
            mismatches = audit_assertions(client)
            for mismatch in mismatches:
                print(mismatch)
            print(f"Found {len(mismatches)} assertion count mismatches")
            return 1 if mismatches else 0
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
