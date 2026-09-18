"""Bounded descriptor readiness: activation gating and capability blocking."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _bounded_ingestion_fixture import FixtureDescriptor, unit
from _whatsadmin_bounded_fixture import (
    CREDENTIAL,
    context,
)
from _whatsadmin_bounded_fixture import occurrence as contract_occurrence
from _whatsadmin_bounded_fixture import scope as contract_scope
from pydantic import SecretStr
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.connectors.registry import BoundedConnectorRegistry
from src.connectors.whatsadmin_api import bounded_descriptor as descriptor_module
from src.connectors.whatsadmin_api.bounded_descriptor import WhatsAdminBoundedDescriptor
from src.connectors.whatsadmin_api.bounded_state import WhatsAdminCursor
from src.connectors.whatsadmin_api.models import CapabilityGuarantee, UpstreamCapability


class BlockedDescriptor(FixtureDescriptor):
    """A registered descriptor whose source capability is not yet proven."""

    def readiness_block(self) -> str | None:
        return "fixture_capability_unproven"


class ReadyDescriptor(FixtureDescriptor):
    def readiness_block(self) -> str | None:
        return None


class BrokenDescriptor(FixtureDescriptor):
    def readiness_block(self) -> str | None:
        raise RuntimeError("readiness probe blew up")


class EmptyReasonDescriptor(FixtureDescriptor):
    def readiness_block(self) -> str | None:
        return "   "


class _UnusedControl:
    """Dispatch never reaches the control once readiness blocks."""


class _Settings(SimpleNamespace):
    """Minimal settings surface used by the bounded descriptor."""


def _register(descriptor: FixtureDescriptor, source_key: str) -> BoundedConnectorRegistry:
    registry = BoundedConnectorRegistry()
    descriptor.source_key = source_key
    registry.register(descriptor)
    return registry


def test_descriptor_without_a_readiness_check_stays_compatible() -> None:
    descriptor = FixtureDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry = _register(descriptor, "fixture-compatible")

    assert registry.require("fixture-compatible", "bootstrap") is descriptor


def test_readiness_gate_blocks_non_ready_descriptors() -> None:
    blocked = BlockedDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry = _register(blocked, "fixture-blocked")

    with pytest.raises(LookupError, match="fixture_capability_unproven"):
        registry.require("fixture-blocked", "bootstrap")
    assert blocked.create_calls == 0
    assert blocked.fetch_calls == 0


def test_ready_descriptor_is_returned_unchanged() -> None:
    ready = ReadyDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry = _register(ready, "fixture-ready")

    assert registry.require("fixture-ready", "bootstrap") is ready


def test_unreadable_readiness_fails_closed() -> None:
    broken = BrokenDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry = _register(broken, "fixture-broken")

    with pytest.raises(LookupError, match="readiness check failed"):
        registry.require("fixture-broken", "bootstrap")


def test_empty_readiness_reason_fails_closed() -> None:
    empty = EmptyReasonDescriptor({0: unit(0, (("identity-1", "v1"),))})
    registry = _register(empty, "fixture-empty")

    with pytest.raises(LookupError, match="empty reason"):
        registry.require("fixture-empty", "bootstrap")


def test_production_descriptor_is_blocked_on_unproven_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = BoundedConnectorRegistry()
    registry.register(WhatsAdminBoundedDescriptor())

    with pytest.raises(LookupError) as excinfo:
        registry.require("whatsapp_chat", "delta")

    message = str(excinfo.value)
    assert message.startswith("whatsadmin_bounded_capability_unproven:")
    assert "requested_as_of_snapshot_binding" in message
    assert "chat_session_removal_tombstones" in message
    assert "removed_versus_unavailable_distinction" in message


def test_blocked_descriptor_reports_capability_through_dispatch() -> None:
    from src.bounded_ingestion_dispatch import dispatch_one
    from src.bounded_ingestion_models import BoundedRunResult

    registry = BoundedConnectorRegistry()
    registry.register(WhatsAdminBoundedDescriptor())
    scope = contract_scope()

    result: BoundedRunResult = dispatch_one(
        registry=registry,
        control=_UnusedControl(),  # type: ignore[arg-type]
        scope=scope,
        occurrence=contract_occurrence(),
        worker_task_id="task-1",
        budget=BoundedIngestionBudget(),
    )

    assert result.status == "blocked"
    assert result.failure_category == "capability"
    assert (result.safe_message or "").startswith("whatsadmin_bounded_capability_unproven:")


def test_proven_capability_clears_the_readiness_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proven = UpstreamCapability(
        contract_version=descriptor_module.CONTRACT_VERSION,
        cursor_retention_days=45,
        guarantees=tuple(
            CapabilityGuarantee(name=name, proven=True, evidence="deployed probe")
            for name in descriptor_module.UPSTREAM_CAPABILITY.guarantees
        ),
    )
    monkeypatch.setattr(descriptor_module, "UPSTREAM_CAPABILITY", proven)
    descriptor = WhatsAdminBoundedDescriptor()

    assert descriptor.readiness_block() is None

    registry = BoundedConnectorRegistry()
    registry.register(descriptor)
    assert registry.require("whatsapp_chat", "delta") is descriptor


def test_contract_version_drift_blocks_even_a_proven_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = UpstreamCapability(
        contract_version="whatsadmin-hyperp-extraction-v2",
        cursor_retention_days=45,
        guarantees=tuple(
            CapabilityGuarantee(name=name, proven=True, evidence="deployed probe")
            for name in descriptor_module.UPSTREAM_CAPABILITY.guarantees
        ),
    )
    monkeypatch.setattr(descriptor_module, "UPSTREAM_CAPABILITY", drifted)

    assert (
        WhatsAdminBoundedDescriptor().readiness_block()
        == "whatsadmin_bounded_contract_version_mismatch"
    )


def test_initial_checkpoint_copies_the_scope_window_without_upstream_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(descriptor_module, "get_settings", _settings)
    descriptor = WhatsAdminBoundedDescriptor()

    checkpoint = descriptor.initial_checkpoint(contract_scope(), None)

    assert checkpoint.phase == "whatsadmin"
    assert checkpoint.source_window == contract_scope().source_window
    assert checkpoint.connector_version == descriptor.connector_version
    assert checkpoint.schema_version == descriptor.checkpoint_schema_version
    cursor = WhatsAdminCursor.from_payload(checkpoint.cursor)
    assert cursor.subphase == "sessions"
    assert cursor.credential_fingerprint.startswith("sha256:")


def test_initial_checkpoint_rejects_a_malformed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(descriptor_module, "get_settings", _settings)
    scope = contract_scope()

    class _BrokenScope(SimpleNamespace):
        pass

    broken = _BrokenScope(**{**scope.__dict__, "source_window": {"window_id": "x"}})
    with pytest.raises(ValueError, match="invalid shape"):
        WhatsAdminBoundedDescriptor().initial_checkpoint(broken, None)  # type: ignore[arg-type]


def test_create_builds_an_entity_bound_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(descriptor_module, "get_settings", _settings)
    monkeypatch.setattr(descriptor_module, "bounded_graph_client", lambda: object())
    descriptor = WhatsAdminBoundedDescriptor()
    scope = contract_scope()
    checkpoint = descriptor.initial_checkpoint(scope, None)

    connector = descriptor.create(context(checkpoint, run_scope=scope))

    assert connector.validate_checkpoint(checkpoint) == "compatible"
    connector.close()


def _settings() -> _Settings:
    return _Settings(
        whatsadmin_api_base_url="https://whatsadmin.test",
        whatsadmin_eko_api_key=SecretStr(CREDENTIAL),
        whatsadmin_speedzone_api_key=SecretStr("hk_speedzone_fixture_secret"),
        whatsadmin_eko_enabled=True,
        whatsadmin_speedzone_enabled=True,
        whatsadmin_legacy_entity=None,
        whatsadmin_api_page_size=25,
        whatsadmin_api_timeout_seconds=120.0,
        whatsadmin_api_max_attempts=5,
        whatsadmin_api_retry_base_delay_seconds=1.0,
    )
