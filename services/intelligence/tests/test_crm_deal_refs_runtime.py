"""#352 admission/publication coverage for one injected CRM deal-reference command."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
from intelligence.config import RuntimeConfig
from intelligence.crm_deal_refs.export import export_snapshot
from intelligence.crm_deal_refs.models import Boundary, DealKey, DealReference, utc_now
from intelligence.registry import RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime


def _handler(staging: Path, _cancelled: object) -> None:
    captured = utc_now()
    deal = DealReference(
        source_system="bitrix_chat",
        source_instance_id="instance-a",
        identity_policy_version="crm_deal_identity_v2",
        key=DealKey("deal-1", 1, "pk-1"),
        record_hash="a" * 64,
        source_entity_type="deal",
        source_entity_id="1",
        entity_key=None,
        category_id="1",
        stage_id="C1:NEW",
        stage_semantic_id=None,
        source_outcome_ref=None,
        observed_at="2026-01-01T00:00:00Z",
        available_at="2026-01-01T00:00:00Z",
        first_known_at="2026-01-01T00:00:00Z",
        source_event_at="2026-01-01T00:00:00Z",
        source_effective_at="2026-01-01T00:00:00Z",
        source_close_date=None,
        availability="known_by_cutoff",
        point_in_time_eligible=True,
        lifecycle_status_observed="active",
        link_status_observed="unresolved",
        observation_captured_at=captured,
    )
    from intelligence.crm_deal_refs.models import canonical_digest

    boundary = Boundary(
        2,
        "crm-deal-refs-v2",
        "bitrix_chat",
        "instance-a",
        "crm_deal_identity_v2",
        "2026-02-01T00:00:00Z",
        captured,
        100,
        10,
        0,
        1,
        deal.key,
        canonical_digest([asdict(deal.key)]),
        canonical_digest(
            [
                {
                    key: value
                    for key, value in asdict(deal).items()
                    if key
                    not in {
                        "lifecycle_status_observed",
                        "link_status_observed",
                        "observation_captured_at",
                    }
                }
            ]
        ),
        canonical_digest([]),
        canonical_digest(
            [
                {
                    "key": {
                        "source_record_id": "deal-1",
                        "source_record_version": 1,
                        "source_record_pk": "pk-1",
                    },
                    "lifecycle_status_observed": "active",
                    "link_status_observed": "unresolved",
                }
            ]
        ),
    )
    export_snapshot(staging, boundary, (deal,), ())


def test_default_off_and_injected_runtime_publication(tmp_path: Path) -> None:
    command = RegisteredCommand("crm_deal_refs_extract", True, _handler, {})
    disabled = IntelligenceRuntime(RuntimeConfig(tmp_path), Registry((command,)))
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            disabled.run(command.name)
    finally:
        disabled.close()
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True), Registry((command,))
    )
    try:
        run_id = runtime.run(command.name)
        assert runtime.state.inspect(run_id) is not None
        assert runtime.state.accepted_outputs(run_id)
    finally:
        runtime.close()
