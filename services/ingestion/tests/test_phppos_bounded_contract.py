"""Bounded PHPPOS window, cursor, and page contract tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from _phppos_bounded_fixture import (
    CONFIGURATION_FINGERPRINT,
    CONTRACT_VERSION,
    capabilities,
    changes,
    page_payload,
    source_window,
    tombstone,
    window,
    window_payload,
)
from pydantic import ValidationError
from src.connectors.phppos_api.bounded_checkpoint import (
    PhpposCheckpointError,
    PhpposCursor,
    PhpposSourceWindow,
    initial_checkpoint,
    next_checkpoint,
    parse_checkpoint,
    phase_for_resource,
    resource_for_source,
    source_window_mapping,
)
from src.connectors.phppos_api.models import BoundedPage, BoundedWindow
from src.models import JsonValue
from src.resumable import CheckpointDescriptor


def _expired_source_window() -> dict[str, JsonValue]:
    return {
        "source_key": "eko_phppos",
        "tenant_id": "eko-tenant",
        "resource": "customers",
        "configuration_fingerprint": CONFIGURATION_FINGERPRINT,
        "window": window_payload(retention_until="2020-01-01T00:00:00+00:00"),
    }


def test_frozen_window_requires_every_source_capability() -> None:
    admitted = BoundedWindow.model_validate(window_payload())
    assert admitted.retention_until.tzinfo is not None

    for capability in (
        "effective_changes",
        "tombstones",
        "complete_sale_aggregates",
        "independent_tenant_principal",
    ):
        with pytest.raises(ValidationError, match="capability contract is incomplete"):
            BoundedWindow.model_validate(
                window_payload(capability_overrides={capability: False})
            )

    with pytest.raises(ValidationError, match="replay_retention_days"):
        BoundedWindow.model_validate(
            window_payload(capability_overrides={"replay_retention_days": 7})
        )


def test_frozen_window_requires_timezone_aware_retention_and_known_contract() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        BoundedWindow.model_validate(window_payload(retention_until="2026-12-31T00:00:00"))

    with pytest.raises(ValidationError):
        BoundedWindow.model_validate(window_payload() | {"contract_version": "phppos-v2"})


def test_source_window_round_trips_and_rejects_incompatible_scopes() -> None:
    mapping = source_window("eko_phppos")
    parsed = PhpposSourceWindow.from_mapping(
        mapping,
        source_key="eko_phppos",
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
    )
    assert source_window_mapping(parsed) == mapping

    for field_name, value in (
        ("source_key", "speedzone_phppos"),
        ("tenant_id", ""),
        ("resource", "sales"),
        ("configuration_fingerprint", "other-fingerprint"),
    ):
        mutated = dict(mapping) | {field_name: value}
        with pytest.raises(PhpposCheckpointError, match="PHPPOS source window"):
            PhpposSourceWindow.from_mapping(
                mutated,
                source_key="eko_phppos",
                configuration_fingerprint=CONFIGURATION_FINGERPRINT,
            )


def test_expired_source_window_is_reported_as_expired_not_corrupted() -> None:
    with pytest.raises(PhpposCheckpointError) as expired:
        PhpposSourceWindow.from_mapping(
            _expired_source_window(),
            source_key="eko_phppos",
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        )
    assert expired.value.compatibility == "expired"

    malformed = dict(_expired_source_window()) | {"window": "not-an-object"}
    with pytest.raises(PhpposCheckpointError) as corrupted:
        PhpposSourceWindow.from_mapping(
            malformed,
            source_key="eko_phppos",
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        )
    assert corrupted.value.compatibility == "corrupted"


def test_stream_phase_is_fixed_per_resource() -> None:
    assert resource_for_source("eko_phppos") == "customers"
    assert resource_for_source("speedzone_phppos:sales") == "sales"
    assert phase_for_resource("customers") == "phppos_api:customers"
    assert phase_for_resource("sales") == "phppos_api:sales"
    with pytest.raises(PhpposCheckpointError, match="unsupported"):
        resource_for_source("fundbox")


def test_initial_and_next_checkpoints_keep_one_phase_and_a_typed_cursor() -> None:
    frozen = window("eko_phppos")
    before = initial_checkpoint("eko_phppos", frozen)
    assert before.phase == "phppos_api:customers"
    assert before.connector_version == CONTRACT_VERSION
    assert before.source_window == source_window_mapping(frozen)

    parsed_window, cursor = parse_checkpoint(
        before,
        source_key="eko_phppos",
        configuration_fingerprint=CONFIGURATION_FINGERPRINT,
    )
    assert parsed_window == frozen
    assert cursor.page_cursor is None
    assert cursor.record_offset == 0
    assert cursor.terminal is False

    in_page = next_checkpoint(
        before,
        frozen,
        next_page_cursor=None,
        next_record_offset=2,
        last_committed_record_id="4022",
        terminal=False,
    )
    next_page = next_checkpoint(
        before,
        frozen,
        next_page_cursor="opaque-next-page",
        last_committed_record_id="4023",
        terminal=False,
    )
    final = next_checkpoint(
        before,
        frozen,
        next_page_cursor=None,
        last_committed_record_id="4023",
        terminal=True,
    )

    assert in_page.cursor["page_cursor"] is None
    assert in_page.cursor["record_offset"] == 2
    assert next_page.cursor["page_cursor"] == "opaque-next-page"
    assert next_page.cursor["record_offset"] == 0
    assert final.cursor["terminal"] is True
    assert final.cursor["page_cursor"] is None
    assert final.cursor["terminal_marker"] == "terminal:snap-2026-09-17"

    replay_ids = {
        before.replay_boundary,
        in_page.replay_boundary,
        next_page.replay_boundary,
        final.replay_boundary,
    }
    assert len(replay_ids) == 4
    for checkpoint in (in_page, next_page, final):
        parse_checkpoint(
            checkpoint,
            source_key="eko_phppos",
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        )


def test_terminal_checkpoint_cannot_also_continue() -> None:
    frozen = window("eko_phppos")
    before = initial_checkpoint("eko_phppos", frozen)
    with pytest.raises(PhpposCheckpointError, match="cannot continue"):
        next_checkpoint(
            before,
            frozen,
            next_page_cursor="opaque-next-page",
            last_committed_record_id=None,
            terminal=True,
        )
    with pytest.raises(PhpposCheckpointError, match="cannot continue"):
        next_checkpoint(
            before,
            frozen,
            next_page_cursor=None,
            next_record_offset=3,
            last_committed_record_id=None,
            terminal=True,
        )


def test_cursor_rejects_tampering_and_malformed_state() -> None:
    frozen = window("eko_phppos")
    before = initial_checkpoint("eko_phppos", frozen)
    tampered = dict(before.cursor) | {"record_offset": 4}
    with pytest.raises(PhpposCheckpointError) as error:
        PhpposCursor.from_mapping(tampered, frozen)  # type: ignore[arg-type]
    assert error.value.compatibility == "corrupted"

    for mutated in (
        {"terminal": True, "terminal_marker": None},
        {"record_offset": -1},
        {"page_replay_id": ""},
        {"record_offset": True},
    ):
        with pytest.raises(PhpposCheckpointError):
            PhpposCursor.from_mapping(dict(before.cursor) | mutated, frozen)


def test_parse_checkpoint_rejects_version_phase_and_window_drift() -> None:
    frozen = window("eko_phppos")
    before = initial_checkpoint("eko_phppos", frozen)

    def parse(checkpoint: CheckpointDescriptor) -> None:
        parse_checkpoint(
            checkpoint,
            source_key="eko_phppos",
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        )

    parse(before)
    with pytest.raises(PhpposCheckpointError, match="version is incompatible"):
        parse(replace(before, connector_version="other-v1"))
    with pytest.raises(PhpposCheckpointError, match="version is incompatible"):
        parse(replace(before, schema_version=2))
    with pytest.raises(PhpposCheckpointError, match="phase is incompatible"):
        parse(replace(before, phase="phppos_api:sales"))
    with pytest.raises(PhpposCheckpointError, match="replay boundary is incompatible"):
        parse(replace(before, replay_boundary="other-boundary"))
    with pytest.raises(PhpposCheckpointError, match="PHPPOS source window"):
        parse_checkpoint(
            before,
            source_key="eko_phppos",
            configuration_fingerprint="other-fingerprint",
        )
    with pytest.raises(PhpposCheckpointError, match="unsupported"):
        parse_checkpoint(
            before,
            source_key="fundbox",
            configuration_fingerprint=CONFIGURATION_FINGERPRINT,
        )


def test_bounded_page_discriminates_upserts_from_explicit_tombstones() -> None:
    payload = page_payload("customers", data=[*changes("customers", limit=1), tombstone("4099")])
    page = BoundedPage.model_validate(payload)
    assert page.resource == "customers"
    assert page.data[0].kind == "upsert"
    assert page.data[1].kind == "tombstone"
    assert page.pagination.has_more is False

    duplicated = page_payload(
        "customers",
        data=[*changes("customers", limit=1), *changes("customers", limit=1)],
    )
    with pytest.raises(ValidationError, match="repeats a source/version identity"):
        BoundedPage.model_validate(duplicated)


def test_bounded_page_rejects_unknown_kinds_and_cursor_inconsistency() -> None:
    unknown_kind = page_payload(
        "customers",
        data=[{"kind": "skip", "source_id": "1", "effective_change_version": "1"}],
    )
    with pytest.raises(ValidationError):
        BoundedPage.model_validate(unknown_kind)

    inconsistent = page_payload("customers")
    inconsistent["pagination"] = {"next_cursor": None, "has_more": True}
    with pytest.raises(ValidationError, match="next_cursor must be present"):
        BoundedPage.model_validate(inconsistent)


def test_capability_mapping_keeps_declared_values() -> None:
    declared = capabilities(tombstones=False)
    assert declared["tombstones"] is False
    assert set(declared) == {
        "effective_changes",
        "tombstones",
        "complete_sale_aggregates",
        "independent_tenant_principal",
        "replay_retention_days",
    }
