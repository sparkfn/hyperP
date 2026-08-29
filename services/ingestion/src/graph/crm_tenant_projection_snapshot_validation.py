"""Bounded terminal membership-snapshot validation for tenant projection releases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, TypeVar

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
    READ_SNAPSHOT_OBSERVATION_GUARD,
    READ_SNAPSHOT_OBSERVATION_PAGE,
)

_SNAPSHOT_VALIDATION_PAGE_LIMIT = 200
_T = TypeVar("_T")


class _ReadClient(Protocol):
    def execute_read(self, work: Callable[[ManagedTransaction], _T]) -> _T: ...


def _validate_input_snapshot_contents(
    tx: ManagedTransaction,
    release: CrmTenantProjectionReleaseSummary,
    input_id: str,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
) -> None:
    guard = tx.run(
        READ_SNAPSHOT_OBSERVATION_GUARD,
        release_id=release.release_id,
        input_id=input_id,
    ).single()
    _validate_snapshot_guard(guard, release, input_id, snapshot_id, subject_kind, subject_id)
    cursor_observation_id: str | None = None
    cursor_node_id: str | None = None
    rows: list[Mapping[str, object]] = []
    observation_ids: set[str] = set()
    observation_nodes: dict[str, str] = {}
    while True:
        page = list(
            tx.run(
                READ_SNAPSHOT_OBSERVATION_PAGE,
                release_id=release.release_id,
                input_id=input_id,
                cursor_observation_id=cursor_observation_id,
                cursor_node_id=cursor_node_id,
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
                node_id = _mapping_string(row, "observation_node_id")
                _advance_observation_cursor(
                    cursor_observation_id,
                    cursor_node_id,
                    observation_id,
                    node_id,
                )
                cursor_observation_id = observation_id
                cursor_node_id = node_id
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


def _validate_input_snapshot_contents_bounded(
    client: _ReadClient,
    release: CrmTenantProjectionReleaseSummary,
    input_id: str,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
) -> None:
    """Validate one selected snapshot with one bounded read transaction per page."""
    guard = client.execute_read(
        lambda tx: tx.run(
            READ_SNAPSHOT_OBSERVATION_GUARD,
            release_id=release.release_id,
            input_id=input_id,
        ).single()
    )
    _validate_snapshot_guard(guard, release, input_id, snapshot_id, subject_kind, subject_id)
    cursor_observation_id: str | None = None
    cursor_node_id: str | None = None
    rows: list[Mapping[str, object]] = []
    observation_ids: set[str] = set()
    observation_nodes: dict[str, str] = {}
    while True:

        def read_page(
            tx: ManagedTransaction,
            cursor_observation_id: str | None = cursor_observation_id,
            cursor_node_id: str | None = cursor_node_id,
        ) -> list[Mapping[str, object]]:
            return list(
                tx.run(
                    READ_SNAPSHOT_OBSERVATION_PAGE,
                    release_id=release.release_id,
                    input_id=input_id,
                    cursor_observation_id=cursor_observation_id,
                    cursor_node_id=cursor_node_id,
                    page_limit=_SNAPSHOT_VALIDATION_PAGE_LIMIT,
                )
            )

        page = client.execute_read(read_page)
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
                node_id = _mapping_string(row, "observation_node_id")
                _advance_observation_cursor(
                    cursor_observation_id,
                    cursor_node_id,
                    observation_id,
                    node_id,
                )
                cursor_observation_id = observation_id
                cursor_node_id = node_id
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


def _validate_snapshot_guard(
    record: Mapping[str, object] | None,
    release: CrmTenantProjectionReleaseSummary,
    input_id: str,
    snapshot_id: str,
    subject_kind: str,
    subject_id: str,
) -> None:
    if record is None:
        raise CrmTenantProjectionIntegrityError("projection snapshot is missing")
    _validate_snapshot_page_row(record, release, input_id, snapshot_id, subject_kind, subject_id)
    snapshot = _required_mapping(record, "snapshot")
    binding_count = _mapping_int(snapshot, "binding_count")
    for key in (
        "observation_links",
        "observation_nodes",
        "observation_id_count",
        "distinct_observation_ids",
    ):
        if _mapping_int(record, key) != binding_count:
            raise CrmTenantProjectionIntegrityError(
                "membership snapshot observation guard is malformed"
            )


def _advance_observation_cursor(
    prior_observation_id: str | None,
    prior_node_id: str | None,
    observation_id: str,
    node_id: str,
) -> None:
    if prior_observation_id is not None and (
        observation_id < prior_observation_id
        or (
            observation_id == prior_observation_id
            and (prior_node_id is None or node_id <= prior_node_id)
        )
    ):
        raise CrmTenantProjectionIntegrityError(
            "projection snapshot observation cursor is malformed"
        )
