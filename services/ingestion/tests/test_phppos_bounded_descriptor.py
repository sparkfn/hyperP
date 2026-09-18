"""Bounded PHPPOS descriptors: registry discovery, scopes, and admission."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from _phppos_bounded_fixture import (
    attempt_context,
    scope,
    window_payload,
)
from pydantic import SecretStr
from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.connectors.phppos_api import bounded_descriptor as descriptor_module
from src.connectors.phppos_api.bounded_checkpoint import PhpposCheckpointError
from src.connectors.phppos_api.bounded_connector import PhpposBoundedConnector
from src.connectors.phppos_api.bounded_descriptor import (
    CUSTOMER_SCOPES,
    DESCRIPTORS,
    SALES_SCOPES,
    SUPPORTED_SOURCES,
    PhpposBoundedConfigurationError,
    PhpposBoundedDescriptor,
    create_bounded_client,
)
from src.connectors.registry import BoundedConnectorRegistry
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue

PHASES = {
    "eko_phppos": "phppos_api:customers",
    "eko_phppos:sales": "phppos_api:sales",
    "speedzone_phppos": "phppos_api:customers",
    "speedzone_phppos:sales": "phppos_api:sales",
}


def _settings(
    *,
    eko_tenant: str = "eko-tenant",
    speedzone_tenant: str = "speedzone-tenant",
) -> SimpleNamespace:
    return SimpleNamespace(
        phppos_api_base_url="https://pos.example",
        phppos_api_client_id="client-id",
        phppos_api_client_secret=SecretStr("client-secret"),
        phppos_api_page_size=500,
        phppos_api_timeout_seconds=30.0,
        phppos_api_max_attempts=3,
        eko_phppos_api_tenant_id=eko_tenant,
        speedzone_phppos_api_tenant_id=speedzone_tenant,
    )


def test_registry_discovers_exactly_the_four_adapter_local_scopes() -> None:
    registry = BoundedConnectorRegistry(auto_discover=True)

    assert registry.registered_sources() == tuple(sorted(SUPPORTED_SOURCES))
    for source_key in SUPPORTED_SOURCES:
        for mode in ("bootstrap", "delta"):
            descriptor = registry.require(source_key, mode)
            assert descriptor.source_key == source_key
        with pytest.raises(LookupError, match="requested mode"):
            registry.require(source_key, "one_time")


def test_each_scope_owns_an_independent_writer() -> None:
    writers = {descriptor.source_key: id(descriptor.writer) for descriptor in DESCRIPTORS}
    assert set(writers) == set(SUPPORTED_SOURCES)
    assert len(set(writers.values())) == len(SUPPORTED_SOURCES)
    with pytest.raises(ValueError, match="unsupported"):
        PhpposBoundedDescriptor("fundbox")


def test_descriptor_limits_fit_the_shared_bounded_budget() -> None:
    budget = get_ingestion_config().bounded_ingestion
    assert isinstance(budget, BoundedIngestionBudget)

    for descriptor in DESCRIPTORS:
        assert descriptor.supports_bootstrap is True
        assert descriptor.supports_delta is True
        assert descriptor.supports_one_time is False
        assert descriptor.supports_deadline is True
        assert descriptor.supports_cancellation is True
        assert descriptor.max_records_per_unit >= 1
        # One unit must be able to express the whole retry policy: three OAuth
        # attempts, three page attempts, and one re-authorization pair.
        assert descriptor.max_source_requests_per_unit >= 8
        assert descriptor.max_bytes_per_unit >= 1
        assert descriptor.max_extraction_calls_per_unit >= 1
        assert descriptor.checkpoint_schema_version == 1
        assert descriptor.connector_version == "phppos-bounded-v1"
        assert descriptor.configuration_version == "phppos-bounded-config-v1"

        lifecycle = (
            budget.max_unit_seconds
            + descriptor.max_close_seconds
            + (3 * budget.max_graph_transaction_seconds)
        )
        assert lifecycle <= budget.drain_reserve_seconds
        assert descriptor.max_retry_backoff_seconds <= 3600.0


def test_opening_checkpoint_matches_the_scope_window_and_phase() -> None:
    for descriptor in DESCRIPTORS:
        run_scope = scope(descriptor.source_key)
        checkpoint = descriptor.initial_checkpoint(run_scope, None)

        assert checkpoint.phase == PHASES[descriptor.source_key]
        assert checkpoint.source_window == run_scope.source_window
        assert checkpoint.connector_version == run_scope.connector_version
        assert checkpoint.schema_version == run_scope.checkpoint_schema_version
        assert checkpoint.cursor["record_offset"] == 0
        assert checkpoint.cursor["terminal"] is False


def test_opening_checkpoint_fails_closed_on_an_inadmissible_window() -> None:
    descriptor = DESCRIPTORS[0]

    expired = scope(
        "eko_phppos",
        source_window_override=cast(
            dict[str, JsonValue],
            {
                "source_key": "eko_phppos",
                "tenant_id": "eko-tenant",
                "resource": "customers",
                "configuration_fingerprint": scope("eko_phppos").configuration_fingerprint,
                "window": window_payload(
                    retention_until="2020-01-01T00:00:00+00:00",
                    capability_overrides={"tombstones": False},
                ),
            },
        ),
    )
    with pytest.raises(PhpposCheckpointError, match="capabilities are not admitted"):
        descriptor.initial_checkpoint(expired, None)

    incomplete = replace(expired, source_window={"source_key": "eko_phppos"})
    with pytest.raises(PhpposCheckpointError, match="PHPPOS source window"):
        descriptor.initial_checkpoint(incomplete, None)


def test_tenant_scopes_never_share_a_transport_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(descriptor_module, "get_settings", _settings)

    eko_client, eko_tenant = create_bounded_client("eko_phppos", "customers")
    speedzone_client, speedzone_tenant = create_bounded_client(
        "speedzone_phppos:sales", "sales"
    )

    assert eko_tenant == "eko-tenant"
    assert speedzone_tenant == "speedzone-tenant"
    assert eko_tenant != speedzone_tenant
    assert eko_client is not speedzone_client
    eko_client.close()
    speedzone_client.close()


def test_transport_configuration_fails_closed_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(descriptor_module, "get_settings", lambda: _settings(eko_tenant=" "))
    with pytest.raises(PhpposBoundedConfigurationError, match="not configured"):
        create_bounded_client("eko_phppos", "customers")


def test_least_privilege_scopes_are_declared_per_resource() -> None:
    assert CUSTOMER_SCOPES == ("pos.customers.read",)
    assert SALES_SCOPES == ("pos.sales.read", "pos.items.read", "pos.customers.read")


def test_create_wires_a_connector_for_the_scope_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(descriptor_module, "get_settings", _settings)
    descriptor = next(
        item for item in DESCRIPTORS if item.source_key == "speedzone_phppos:sales"
    )
    context = attempt_context("speedzone_phppos:sales")

    connector = descriptor.create(context)

    assert isinstance(connector, PhpposBoundedConnector)
    checkpoint = descriptor.initial_checkpoint(context.scope, None)
    assert connector.validate_checkpoint(checkpoint) == "compatible"
    connector.close()


def test_descriptor_scopes_are_the_approved_eke_and_speedzone_keys() -> None:
    assert SUPPORTED_SOURCES == (
        "eko_phppos",
        "eko_phppos:sales",
        "speedzone_phppos",
        "speedzone_phppos:sales",
    )
    assert {descriptor.source_key: descriptor.resource for descriptor in DESCRIPTORS} == {
        "eko_phppos": "customers",
        "eko_phppos:sales": "sales",
        "speedzone_phppos": "customers",
        "speedzone_phppos:sales": "sales",
    }
