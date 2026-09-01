"""JSON-safe registered operator handlers for #307 lifecycle commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from celery import shared_task

from src.config import get_settings
from src.crm_tenant_mapping_configured_authorization import ConfiguredCrmTenantMappingAuthorizer
from src.crm_tenant_mapping_contracts import (
    CrmTenantMappingAuthorization,
    CrmTenantMappingCompanyEntry,
    CrmTenantMappingExpectedHead,
    CrmTenantMappingManifest,
    CrmTenantMappingScope,
    CrmTenantMappingTarget,
)
from src.crm_tenant_mapping_models import (
    CrmTenantMappingExpectedHeadBoundary,
    CrmTenantMappingPrepareCommand,
    CrmTenantMappingRollbackCommand,
)
from src.crm_tenant_mapping_service import CrmTenantMappingService
from src.crm_tenant_projection_materializer import CrmTenantProjectionMaterializer
from src.crm_tenant_projection_models import CrmTenantProjectionMaterializationCommand
from src.crm_tenant_projection_records import (
    CrmTenantProjectionExpectedHead,
    CrmTenantProjectionScope,
)
from src.graph.client import Neo4jClient
from src.graph.crm_tenant_mapping import Neo4jCrmTenantMappingRepository
from src.graph.crm_tenant_projection import Neo4jCrmTenantProjectionRepository
from src.ingestion_config import get_ingestion_config
from src.standalone_crm_census_tasks import (
    admit_and_run_standalone_crm_census,
    reconcile_standalone_crm_census,
)


def _mapping_service(client: Neo4jClient) -> CrmTenantMappingService:
    return CrmTenantMappingService(
        Neo4jCrmTenantMappingRepository(client),
        ConfiguredCrmTenantMappingAuthorizer(
            get_ingestion_config().crm_tenant_mapping_authorization.grants
        ),
    )


def _obj(value: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("operator payload fields mismatch")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("operator payload text is invalid")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("operator payload integer is invalid")
    return value


def _scope(value: object) -> CrmTenantMappingScope:
    v = _obj(value, {"source_key", "source_instance_id", "control_instance_id"})
    return CrmTenantMappingScope(
        _text(v["source_key"]), _text(v["source_instance_id"]), _text(v["control_instance_id"])
    )


def _boundary(value: object, scope: CrmTenantMappingScope) -> CrmTenantMappingExpectedHeadBoundary:
    v = _obj(value, {"head_id", "expected_head"})
    raw = v["expected_head"]
    head = None if raw is None else _head(raw)
    return CrmTenantMappingExpectedHeadBoundary(scope, _text(v["head_id"]), head)


def _head(value: object) -> CrmTenantMappingExpectedHead:
    v = _obj(
        value, {"head_id", "active_revision_id", "active_revision_number", "active_manifest_digest"}
    )
    return CrmTenantMappingExpectedHead(
        _text(v["head_id"]),
        _text(v["active_revision_id"]),
        _int(v["active_revision_number"]),
        _text(v["active_manifest_digest"]),
    )


def _auth(value: object) -> CrmTenantMappingAuthorization:
    v = _obj(
        value,
        {"actor", "authorization_reference", "authorization_digest", "authorized_at", "expires_at"},
    )
    return CrmTenantMappingAuthorization(
        *(
            _text(v[k])
            for k in (
                "actor",
                "authorization_reference",
                "authorization_digest",
                "authorized_at",
                "expires_at",
            )
        )
    )


def parse_prepare(raw: Mapping[str, object]) -> CrmTenantMappingPrepareCommand:
    v = _obj(
        raw,
        {
            "scope",
            "preparation_request_id",
            "manifest",
            "expected_head_boundary",
            "authorization",
            "operation_time",
        },
    )
    scope = _scope(v["scope"])
    _obj(v["manifest"], {"entries"})
    entries = v["manifest"].get("entries") if isinstance(v["manifest"], Mapping) else None
    if not isinstance(entries, list):
        raise ValueError("operator manifest entries invalid")
    parsed = []
    for entry in entries:
        e = _obj(entry, {"company_id", "targets"})
        targets = e["targets"]
        if not isinstance(targets, list):
            raise ValueError("operator targets invalid")
        parsed.append(
            CrmTenantMappingCompanyEntry(
                _text(e["company_id"]),
                tuple(
                    CrmTenantMappingTarget(_text(_obj(t, {"entity_key"})["entity_key"]))
                    for t in targets
                ),
            )
        )
    return CrmTenantMappingPrepareCommand(
        scope,
        _text(v["preparation_request_id"]),
        CrmTenantMappingManifest(scope, tuple(parsed)),
        _boundary(v["expected_head_boundary"], scope),
        _auth(v["authorization"]),
        _text(v["operation_time"]),
    )


def parse_rollback(raw: Mapping[str, object]) -> CrmTenantMappingRollbackCommand:
    v = _obj(
        raw,
        {
            "scope",
            "preparation_request_id",
            "rollback_of_revision_id",
            "rollback_of_manifest_digest",
            "expected_head_boundary",
            "authorization",
            "operation_time",
        },
    )
    scope = _scope(v["scope"])
    return CrmTenantMappingRollbackCommand(
        scope,
        _text(v["preparation_request_id"]),
        _text(v["rollback_of_revision_id"]),
        _text(v["rollback_of_manifest_digest"]),
        _boundary(v["expected_head_boundary"], scope),
        _auth(v["authorization"]),
        _text(v["operation_time"]),
    )


@shared_task(name="src.crm_tenant_operator_tasks.prepare")  # type: ignore[untyped-decorator]
def prepare(raw: Mapping[str, object]) -> str:
    c = Neo4jClient(get_settings())
    try:
        return _mapping_service(c).prepare(parse_prepare(raw)).revision.revision_id
    finally:
        c.close()


@shared_task(name="src.crm_tenant_operator_tasks.rollback")  # type: ignore[untyped-decorator]
def rollback(raw: Mapping[str, object]) -> str:
    c = Neo4jClient(get_settings())
    try:
        return _mapping_service(c).rollback(parse_rollback(raw)).revision.revision_id
    finally:
        c.close()


@shared_task(name="src.crm_tenant_operator_tasks.activate")  # type: ignore[untyped-decorator]
def activate(raw: dict[str, object]) -> str:
    return cast(str, admit_and_run_standalone_crm_census(raw))


@shared_task(name="src.crm_tenant_operator_tasks.status")  # type: ignore[untyped-decorator]
def status(census_id: str) -> str:
    from src.graph.standalone_crm_census import StandaloneCrmCensusRepository

    c = Neo4jClient(get_settings())
    try:
        value = StandaloneCrmCensusRepository(c).status(census_id)
        return "missing" if value is None else value.state
    finally:
        c.close()


@shared_task(name="src.crm_tenant_operator_tasks.reconcile")  # type: ignore[untyped-decorator]
def reconcile(census_id: str) -> str | None:
    return cast(str | None, reconcile_standalone_crm_census(census_id))


@shared_task(name="src.crm_tenant_operator_tasks.source_sync")  # type: ignore[untyped-decorator]
def source_sync(raw: dict[str, object]) -> str:
    return cast(str, admit_and_run_standalone_crm_census(raw))


def parse_project(raw: Mapping[str, object]) -> CrmTenantProjectionMaterializationCommand:
    keys = {
        "scope",
        "request_id",
        "source_census_id",
        "source_census_fingerprint",
        "mapping_revision_id",
        "mapping_manifest_digest",
        "expected_mapping_head_boundary",
        "expected_prior_head",
        "page_limit",
    }
    v = _obj(raw, keys)
    scope = _scope(v["scope"])
    projection_scope = CrmTenantProjectionScope(
        scope.source_key, scope.source_instance_id, scope.control_instance_id
    )
    prior_raw = v["expected_prior_head"]
    prior = None
    if prior_raw is not None:
        p = _obj(
            prior_raw,
            {"head_id", "active_release_id", "active_release_number", "active_release_fingerprint"},
        )
        prior = CrmTenantProjectionExpectedHead(
            _text(p["head_id"]),
            _text(p["active_release_id"]),
            _int(p["active_release_number"]),
            _text(p["active_release_fingerprint"]),
        )
    return CrmTenantProjectionMaterializationCommand(
        projection_scope,
        _text(v["request_id"]),
        _text(v["source_census_id"]),
        _text(v["source_census_fingerprint"]),
        _text(v["mapping_revision_id"]),
        _text(v["mapping_manifest_digest"]),
        _boundary(v["expected_mapping_head_boundary"], scope),
        prior,
        _int(v["page_limit"]),
    )


@shared_task(name="src.crm_tenant_operator_tasks.project")  # type: ignore[untyped-decorator]
def project(raw: Mapping[str, object]) -> str:
    client = Neo4jClient(get_settings())
    try:
        mapping = Neo4jCrmTenantMappingRepository(client)
        projection = Neo4jCrmTenantProjectionRepository(client)
        return (
            CrmTenantProjectionMaterializer(projection, mapping)
            .materialize(parse_project(raw))
            .release_id
        )
    finally:
        client.close()
