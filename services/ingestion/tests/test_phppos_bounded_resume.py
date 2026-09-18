"""Bounded PHPPOS traversal: page slices, in-page continuation, and resume."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest
from _phppos_bounded_fixture import (
    CONFIGURATION_FINGERPRINT,
    OBSERVED_AT,
    attempt_context,
    changes,
    client,
    initial_phppos_checkpoint,
    page_payload,
    rows,
    scripted_handler,
    source_window,
    tenant_for,
    tombstone,
    window,
)
from src.connectors.phppos_api.bounded_checkpoint import (
    resource_for_source,
    source_window_mapping,
)
from src.connectors.phppos_api.bounded_connector import (
    MAX_SALE_LINES,
    PhpposBoundedConnector,
    base_source_key,
    source_record_id,
)
from src.connectors.phppos_api.bounded_descriptor import DESCRIPTORS
from src.connectors.phppos_api.client import (
    PhpposApiClient,
    PhpposBoundedTransportError,
)
from src.models import JsonValue

EKO = "eko_phppos"
SPEEDZONE_SALES = "speedzone_phppos:sales"


def _descriptor(source_key: str) -> object:
    return next(item for item in DESCRIPTORS if item.source_key == source_key)


def _connector(
    api: PhpposApiClient,
    source_key: str = EKO,
    *,
    expected_tenant_id: str | None = None,
    max_records: int | None = None,
) -> PhpposBoundedConnector:
    """Build a connector with the shipped adapter limits under test."""
    descriptor = _descriptor(source_key)
    return PhpposBoundedConnector(
        source_key=source_key,
        resource=resource_for_source(source_key),
        expected_tenant_id=expected_tenant_id or tenant_for(source_key),
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        client=api,
        max_records_per_unit=max_records or descriptor.max_records_per_unit,
        max_source_requests_per_unit=descriptor.max_source_requests_per_unit,
        max_bytes_per_unit=descriptor.max_bytes_per_unit,
        observed_at=lambda: OBSERVED_AT,
    )


def _customer_connector(
    payloads: list[dict[str, JsonValue]],
    *,
    source_key: str = EKO,
    expected_tenant_id: str | None = None,
    max_records: int | None = None,
) -> PhpposBoundedConnector:
    return _connector(
        client(scripted_handler(payloads, [])),
        source_key,
        expected_tenant_id=expected_tenant_id,
        max_records=max_records,
    )


def test_first_unit_maps_the_frozen_page_through_the_canonical_mapper() -> None:
    connector = _customer_connector([page_payload("customers")])
    context = attempt_context()

    unit = connector.fetch_one_unit(context.checkpoint, context)

    assert unit.unit.checkpoint_before == context.checkpoint
    assert unit.terminal is True
    assert unit.replay_id == context.checkpoint.replay_boundary
    assert len(unit.unit.records) == len(changes("customers"))
    assert unit.usage.records == len(unit.unit.records)
    assert unit.usage.pages == 1
    assert unit.usage.source_requests == 2

    first = unit.unit.records[0]
    assert first["record_type"] == "identity"
    assert first["source_record_id"] == source_record_id(EKO, "customers", "4021")
    assert cast(str, first["observed_at"]).startswith("2026-09-01T04:15:00")
    assert cast(dict[str, JsonValue], first["attributes"])["full_name"] == "Tan Wei Ming"

    terminal = unit.unit.checkpoint_after.cursor
    assert terminal["terminal"] is True
    assert terminal["page_cursor"] is None


def test_an_unexhausted_page_continues_in_page_at_a_bounded_offset() -> None:
    connector = _customer_connector([page_payload("customers")], max_records=2)
    context = attempt_context()

    first = connector.fetch_one_unit(context.checkpoint, context)
    assert first.terminal is False
    assert [record["source_record_id"] for record in first.unit.records] == [
        source_record_id(EKO, "customers", "4021"),
        source_record_id(EKO, "customers", "4022"),
    ]
    assert first.unit.checkpoint_after.cursor["record_offset"] == 2
    assert first.unit.checkpoint_after.cursor["page_cursor"] is None
    assert first.unit.checkpoint_after.last_committed_record_id == "4022"

    second = connector.fetch_one_unit(first.unit.checkpoint_after, context)
    assert second.terminal is True
    assert [record["source_record_id"] for record in second.unit.records] == [
        source_record_id(EKO, "customers", "4023"),
    ]
    assert second.unit.checkpoint_after.cursor["terminal"] is True
    assert second.replay_id != first.replay_id


def test_a_following_page_advances_the_page_cursor_and_resets_the_offset() -> None:
    connector = _customer_connector(
        [
            page_payload("customers", data=changes("customers", limit=1), next_cursor="page-2"),
            page_payload("customers", data=changes("customers", limit=1)),
        ]
    )
    context = attempt_context()

    first = connector.fetch_one_unit(context.checkpoint, context)
    assert first.terminal is False
    assert first.unit.checkpoint_after.cursor["page_cursor"] == "page-2"
    assert first.unit.checkpoint_after.cursor["record_offset"] == 0

    second = connector.fetch_one_unit(first.unit.checkpoint_after, context)
    assert second.terminal is True
    assert second.unit.checkpoint_after.cursor["page_cursor"] is None


def test_replaying_the_same_frozen_page_is_deterministic() -> None:
    payload = page_payload(
        "customers",
        data=[*changes("customers", limit=1), tombstone("4099")],
    )
    first = _customer_connector([payload])
    second = _customer_connector([payload])
    context = attempt_context()

    left = first.fetch_one_unit(context.checkpoint, context)
    right = second.fetch_one_unit(context.checkpoint, context)

    assert left.unit.records == right.unit.records
    assert left.replay_id == right.replay_id
    assert left.unit.checkpoint_after == right.unit.checkpoint_after
    assert right.unit.records[1]["_retire_source_record_id"] == source_record_id(
        EKO, "customers", "4099"
    )
    assert right.unit.records[1]["_retired_at"] == OBSERVED_AT.isoformat()


def test_tombstone_records_retire_the_same_identity_the_upsert_envelope_used() -> None:
    payload = page_payload(
        "sales",
        data=[*changes("sales", limit=1), tombstone("90211")],
        source_key=SPEEDZONE_SALES,
    )
    connector = _connector(
        client(
            scripted_handler([payload], []),
            tenant_id=tenant_for(SPEEDZONE_SALES),
        ),
        SPEEDZONE_SALES,
    )
    context = attempt_context(SPEEDZONE_SALES)

    unit = connector.fetch_one_unit(context.checkpoint, context)

    upsert, removed = unit.unit.records
    assert upsert["record_type"] == "sales"
    # The mapper and the tombstone share the tenant's base source key, exactly as
    # the legacy dump and direct-DB sales connectors do.
    assert upsert["source_record_id"] == "speedzone_phppos-sale-90211"
    assert base_source_key(SPEEDZONE_SALES) == "speedzone_phppos"
    assert source_record_id(base_source_key(SPEEDZONE_SALES), "sales", "90211") == (
        upsert["source_record_id"]
    )
    assert removed["_retire_source_record_id"] == upsert["source_record_id"]
    assert context.scope.source_key == SPEEDZONE_SALES


def test_sale_aggregate_bounds_and_identity_consistency_fail_closed() -> None:
    oversized = rows("sale_rows.json")[0] | {
        "lines": [
            {"sale_id": 90211, "line": index + 1, "item_id": 8801 + index}
            for index in range(MAX_SALE_LINES + 1)
        ]
    }
    inconsistent = rows("sale_rows.json")[0] | {
        "lines": [{"sale_id": 99999, "line": 1, "item_id": 8801}]
    }
    mismatched_identity = [
        {
            "kind": "upsert",
            "source_id": "99999",
            "effective_change_version": "4120",
            "record": rows("sale_rows.json")[0],
        }
    ]

    for data, message in (
        ([_upsert(oversized)], "too large"),
        ([_upsert(inconsistent)], "incomplete"),
        (mismatched_identity, "identity is inconsistent"),
    ):
        payload = page_payload("sales", data=data, source_key=EKO)
        connector = _customer_connector([payload], source_key="eko_phppos:sales")
        context = attempt_context("eko_phppos:sales")
        with pytest.raises(PhpposBoundedTransportError, match=message):
            connector.fetch_one_unit(context.checkpoint, context)


def test_customer_identity_consistency_fails_closed() -> None:
    data = [
        {
            "kind": "upsert",
            "source_id": "99999",
            "effective_change_version": "4120",
            "record": rows("customer_rows.json")[0],
        }
    ]
    connector = _customer_connector([page_payload("customers", data=data)])
    context = attempt_context()

    with pytest.raises(PhpposBoundedTransportError, match="identity is inconsistent"):
        connector.fetch_one_unit(context.checkpoint, context)


def test_page_from_another_tenant_is_refused_by_the_connector() -> None:
    connector = _customer_connector(
        [page_payload("customers")],
        expected_tenant_id="other-tenant",
    )
    context = attempt_context()

    with pytest.raises(PhpposBoundedTransportError, match="crossed its tenant"):
        connector.fetch_one_unit(context.checkpoint, context)


def test_fetching_after_a_terminal_checkpoint_fails_closed() -> None:
    connector = _customer_connector([page_payload("customers")])
    context = attempt_context()
    unit = connector.fetch_one_unit(context.checkpoint, context)

    with pytest.raises(PhpposBoundedTransportError, match="already complete"):
        connector.fetch_one_unit(unit.unit.checkpoint_after, context)


def test_validate_checkpoint_reports_the_exact_incompatibility() -> None:
    connector = _customer_connector([page_payload("customers")])
    compatible = initial_phppos_checkpoint(EKO)
    assert connector.validate_checkpoint(compatible) == "compatible"

    tampered = replace(compatible, cursor=dict(compatible.cursor) | {"record_offset": 5})
    assert connector.validate_checkpoint(tampered) == "corrupted"

    foreign = replace(
        compatible,
        source_window=source_window("speedzone_phppos"),
    )
    assert connector.validate_checkpoint(foreign) == "incompatible"

    expired_window = cast(
        dict[str, JsonValue],
        {
            "source_key": EKO,
            "tenant_id": tenant_for(EKO),
            "resource": "customers",
            "configuration_fingerprint": CONFIGURATION_FINGERPRINT,
            "window": {
                "contract_version": "phppos-bounded-v1",
                "snapshot_id": "snap-2026-09-17",
                "upper_change_version": "4127",
                "retention_until": "2020-01-01T00:00:00+00:00",
                "capabilities": {
                    "effective_changes": True,
                    "tombstones": True,
                    "complete_sale_aggregates": True,
                    "independent_tenant_principal": True,
                    "replay_retention_days": 90,
                },
            },
        },
    )
    assert connector.validate_checkpoint(replace(compatible, source_window=expired_window)) == (
        "expired"
    )
    assert connector.validate_checkpoint(replace(compatible, phase="phppos_api:sales")) == (
        "incompatible"
    )


def test_unit_usage_and_cursor_stay_inside_the_declared_adapter_limits() -> None:
    connector = _customer_connector([page_payload("customers")], max_records=2)
    descriptor = _descriptor(EKO)
    context = attempt_context()

    unit = connector.fetch_one_unit(context.checkpoint, context)

    assert unit.usage.records == len(unit.unit.records) == 2
    assert unit.usage.source_requests <= descriptor.max_source_requests_per_unit
    assert unit.usage.bytes_read <= descriptor.max_bytes_per_unit
    assert unit.usage.extraction_calls == 0
    assert unit.unit.checkpoint_before != unit.unit.checkpoint_after
    assert unit.unit.checkpoint_before.phase == unit.unit.checkpoint_after.phase == (
        "phppos_api:customers"
    )


def test_opening_checkpoint_is_bound_to_the_scope_frozen_window() -> None:
    context = attempt_context()

    assert source_window_mapping(window(EKO)) == context.scope.source_window
    assert context.checkpoint.source_window == context.scope.source_window
    assert context.checkpoint.phase == "phppos_api:customers"
    assert context.checkpoint.connector_version == context.scope.connector_version


def _upsert(record: dict[str, JsonValue], source_id: str = "90211") -> dict[str, JsonValue]:
    return {
        "kind": "upsert",
        "source_id": source_id,
        "effective_change_version": "4120",
        "record": record,
    }
