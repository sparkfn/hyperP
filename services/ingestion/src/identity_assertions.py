"""Immutable ASSERTS edges derived from normalized SourceRecord payloads."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
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


@dataclass(frozen=True)
class AssertionRows:
    """Deduplicated target upsert and assertion rows for one SourceRecord."""

    identifiers: list[dict[str, JsonValue]]
    addresses: list[dict[str, JsonValue]]
    facts: list[dict[str, JsonValue]]


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


def assertion_rows(
    source_record_pk: str,
    normalized_payload: Mapping[str, JsonValue],
) -> AssertionRows:
    """Build stable rows for usable identifiers, addresses, and selected facts."""
    identifiers: dict[str, dict[str, JsonValue]] = {}
    for raw in _items(normalized_payload.get("identifiers")):
        row = _identifier_row(source_record_pk, raw)
        if row is not None:
            identifiers[str(row["assertion_id"])] = row

    addresses: dict[str, dict[str, JsonValue]] = {}
    for raw in _address_items(normalized_payload):
        row = _address_row(source_record_pk, raw)
        if row is not None:
            addresses[str(row["assertion_id"])] = row

    facts: dict[str, dict[str, JsonValue]] = {}
    for raw in _items(normalized_payload.get("attributes")):
        row = _fact_row(source_record_pk, raw)
        if row is not None:
            facts[str(row["assertion_id"])] = row

    return AssertionRows(
        identifiers=[identifiers[key] for key in sorted(identifiers)],
        addresses=[addresses[key] for key in sorted(addresses)],
        facts=[facts[key] for key in sorted(facts)],
    )


def _identifier_row(
    source_record_pk: str,
    raw: Mapping[str, JsonValue],
) -> dict[str, JsonValue] | None:
    identifier_type = _string(raw, "identifier_type")
    normalized_value = _string(raw, "normalized_value")
    quality_flag = _usable_quality(raw.get("quality_flag"))
    if identifier_type is None or normalized_value is None or not quality_flag:
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
    if not _usable_quality(raw.get("quality_flag")):
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
    if attribute_name not in FACT_ASSERTION_ATTRIBUTES or attribute_value is None:
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


def _items(value: JsonValue | None) -> list[Mapping[str, JsonValue]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _address_items(payload: Mapping[str, JsonValue]) -> list[Mapping[str, JsonValue]]:
    values = _items(payload.get("addresses"))
    if values:
        return values
    address = payload.get("address")
    return [address] if isinstance(address, dict) else []


def _string(row: Mapping[str, JsonValue], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) else None


def _optional_string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _usable_quality(value: JsonValue | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return is_usable(QualityFlag(value))
    except ValueError:
        return False


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
    parsed = _parse_normalized_payload({"identifiers": value})
    return parsed if parsed is not None else {"identifiers": []}


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


def backfill_assertions(client: Neo4jClient, batch_size: int = 500) -> int:
    """Backfill ASSERTS for active, pending, parked, and legacy-current records."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    after_source_record_pk: str | None = None
    processed = 0
    while True:
        cursor = after_source_record_pk
        page = client.execute_read(
            lambda tx, _cursor=cursor: _read_page(tx, BACKFILL_ASSERTION_PAGE, _cursor, batch_size)
        )
        if not page:
            return processed
        for record in page:
            source_record_pk = record.get("source_record_pk")
            if not isinstance(source_record_pk, str):
                continue
            payload = _parse_normalized_payload(record.get("normalized_payload"))
            if payload is None:
                payload = _fallback_payload(record.get("fallback_identifiers"))
            client.execute_write(
                lambda tx, _pk=source_record_pk, _payload=payload: write_assertions(
                    tx, _pk, _payload
                )
            )
            processed += 1
        last_pk = page[-1].get("source_record_pk")
        if not isinstance(last_pk, str):
            return processed
        after_source_record_pk = last_pk


def _expected_rows(record: Mapping[str, object], source_record_pk: str) -> AssertionRows:
    payload = _parse_normalized_payload(record.get("normalized_payload"))
    if payload is None:
        payload = _fallback_payload(record.get("fallback_identifiers"))
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
        cursor = after_source_record_pk
        page = client.execute_read(
            lambda tx, _cursor=cursor: _read_page(tx, AUDIT_ASSERTION_PAGE, _cursor, batch_size)
        )
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
