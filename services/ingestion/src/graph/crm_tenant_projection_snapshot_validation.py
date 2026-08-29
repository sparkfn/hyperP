"""Bounded terminal membership-snapshot validation for tenant projection releases."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction

from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionReleaseSummary,
)
from src.graph.crm_tenant_projection_observations import (
    _validate_snapshot_contents,
    _validated_observation_id,
)
from src.graph.crm_tenant_projection_values import (
    _mapping_int,
    _mapping_string,
    _required_mapping,
    _subject_kind_value,
)
from src.graph.queries.crm_tenant_projection_release_pages import (
    READ_SNAPSHOT_OBSERVATION_PAGE,
)

_SNAPSHOT_VALIDATION_PAGE_LIMIT = 200


def _validate_input_snapshot_contents(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
    input_id: str,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
) -> None:
    cursor: str | None = None
    rows: list[Mapping[str, object]] = []
    observation_ids: set[str] = set()
    observation_nodes: dict[str, str] = {}
    while True:
        page = list(
            tx.run(
                READ_SNAPSHOT_OBSERVATION_PAGE,
                release_id=release.release_id,
                input_id=input_id,
                cursor=cursor,
                page_limit=_SNAPSHOT_VALIDATION_PAGE_LIMIT,
            )
        )
        for row in page:
            _validate_snapshot_page_row(
                row, release, input_id, snapshot_id, subject_kind, subject_id
            )
            observation_id = _validated_observation_id(
                row,
                snapshot_id,
                _subject_kind_value(subject_kind),
                subject_id,
                release.scope.source_key,
                release.scope.source_instance_id,
                release.scope.control_instance_id,
                observation_nodes,
            )
            if observation_id is not None:
                _advance_observation_cursor(cursor, observation_id)
                cursor = observation_id
                observation_ids.add(observation_id)
            rows.append(row)
        if len(page) < _SNAPSHOT_VALIDATION_PAGE_LIMIT:
            _validate_snapshot_contents(
                rows,
                snapshot_id,
                _subject_kind_value(subject_kind),
                subject_id,
                release.scope.source_key,
                release.scope.source_instance_id,
                release.scope.control_instance_id,
                observation_ids,
            )
            return


def _validate_snapshot_page_row(
    row: Mapping[str, object],
    release: CrmTenantProjectionReleaseSummary,
    input_id: str,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
) -> None:
    input_node = _required_mapping(row, "input")
    snapshot = _required_mapping(row, "snapshot")
    if (
        _mapping_int(row, "input_owner_links") != 1
        or _mapping_int(row, "input_owner_count") != 1
        or _mapping_int(row, "snapshot_links") != 1
        or _mapping_string(input_node, "input_id") != input_id
        or _mapping_string(input_node, "snapshot_id") != snapshot_id
        or _mapping_string(snapshot, "snapshot_id") != snapshot_id
        or _mapping_string(snapshot, "subject_kind") != subject_kind
        or _mapping_string(snapshot, "subject_id") != subject_id
        or _mapping_string(snapshot, "source_instance_id") != release.scope.source_instance_id
        or _mapping_string(snapshot, "control_instance_id") != release.scope.control_instance_id
    ):
        raise CrmTenantProjectionIntegrityError("projection snapshot ownership is malformed")


def _advance_observation_cursor(cursor: str | None, observation_id: str) -> None:
    if cursor is not None and observation_id <= cursor:
        raise CrmTenantProjectionIntegrityError(
            "projection snapshot observation cursor is malformed"
        )
