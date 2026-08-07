"""Bounded, read-only traversal evidence for Bitrix stage history."""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Literal, Protocol

from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    encode_stage_source_record_id,
    normalize_source_contract_id,
)
from src.connectors.bitrix_stage_history.models import (
    ProbeLimits,
    StageHistoryItem,
    StageHistoryPage,
)
from src.connectors.bitrix_stage_history.reconciliation_spool import (
    CapabilityReconciliationSpool,
    ReconciliationSummary,
    RedactionKey,
    digest_value,
)
from src.connectors.bitrix_stage_history.spool import RestrictedSpool, spool_storage_bytes
from src.models import JsonValue

TraversalMode = Literal["offset", "id_keyset"]
HistoryIdOrdering = Literal["numeric", "lexical"]
_BITRIX_PAGE_SIZE = 50


class StageHistoryClient(Protocol):
    """The small, read-only client surface required by the probe."""

    def list_stage_history_page(
        self,
        *,
        entity_type_id: int,
        filters: Mapping[str, JsonValue] | None = None,
        order_direction: str = "ASC",
        start: int = -1,
    ) -> StageHistoryPage: ...


@dataclass(frozen=True)
class PassManifest:
    """Redactable accounting result for one full bounded traversal pass."""

    traversal_mode: TraversalMode
    calls: int
    raw_rows: int
    unique_identity_rows: int
    duplicate_same_hash_rows: int
    duplicate_conflict_rows: int
    pages: int
    source_total: int | None
    source_total_consistent: bool
    source_total_matches_rows: bool | None
    history_id_ordering: HistoryIdOrdering | None
    minimum_history_id: str | None
    maximum_history_id: str | None
    identity_hash_digest: str
    runtime_seconds: float
    spool_bytes: int
    upper_history_id_digest: str | None = None
    owner_manifest_digest: str | None = None
    global_rows: int | None = None
    in_scope_rows: int | None = None
    out_of_scope_rows: int | None = None
    owners_without_history: int | None = None
    in_scope_identity_hash_digest: str | None = None
    category_inventory_digest: str | None = None
    stage_inventory_digest: str | None = None
    equal_time_group_digest: str | None = None
    operating_seconds: float = 0.0
    operating_samples: int = 0
    latest_operating_reset_at: float | None = None
    current_catalog_stage_count: int | None = None
    in_scope_historical_stage_count: int | None = None
    in_scope_historical_stage_missing_catalog_count: int | None = None
    in_scope_rows_missing_stage_identity: int | None = None

    def to_dict(self) -> dict[str, int | float | str | bool | None]:
        return {
            "traversal_mode": self.traversal_mode,
            "calls": self.calls,
            "raw_rows": self.raw_rows,
            "unique_identity_rows": self.unique_identity_rows,
            "duplicate_same_hash_rows": self.duplicate_same_hash_rows,
            "duplicate_conflict_rows": self.duplicate_conflict_rows,
            "pages": self.pages,
            "source_total": self.source_total,
            "source_total_consistent": self.source_total_consistent,
            "source_total_matches_rows": self.source_total_matches_rows,
            "history_id_ordering": self.history_id_ordering,
            "history_id_bounds_redacted": True,
            "identity_hash_digest": self.identity_hash_digest,
            "runtime_seconds": self.runtime_seconds,
            "spool_bytes": self.spool_bytes,
            "upper_history_id_redacted": self.upper_history_id_digest is not None,
            "upper_history_id_digest": self.upper_history_id_digest,
            "owner_manifest_digest": self.owner_manifest_digest,
            "global_rows": self.global_rows,
            "in_scope_rows": self.in_scope_rows,
            "out_of_scope_rows": self.out_of_scope_rows,
            "owners_without_history": self.owners_without_history,
            "in_scope_identity_hash_digest": self.in_scope_identity_hash_digest,
            "category_inventory_digest": self.category_inventory_digest,
            "stage_inventory_digest": self.stage_inventory_digest,
            "equal_time_group_digest": self.equal_time_group_digest,
            "operating_seconds": self.operating_seconds,
            "operating_samples": self.operating_samples,
            "latest_operating_reset_at": self.latest_operating_reset_at,
            "current_catalog_stage_count": self.current_catalog_stage_count,
            "in_scope_historical_stage_count": self.in_scope_historical_stage_count,
            "in_scope_historical_stage_missing_catalog_count": (
                self.in_scope_historical_stage_missing_catalog_count
            ),
            "in_scope_rows_missing_stage_identity": self.in_scope_rows_missing_stage_identity,
        }


@dataclass
class _HistoryIdBounds:
    lexical_minimum: str | None = None
    lexical_maximum: str | None = None
    numeric_minimum: tuple[int, str] | None = None
    numeric_maximum: tuple[int, str] | None = None
    all_numeric: bool = True

    def add(self, value: str) -> None:
        self.lexical_minimum = (
            value if self.lexical_minimum is None else min(self.lexical_minimum, value)
        )
        self.lexical_maximum = (
            value if self.lexical_maximum is None else max(self.lexical_maximum, value)
        )
        if not value.isdigit():
            self.all_numeric = False
            return
        numeric_value = (int(value), value)
        self.numeric_minimum = (
            numeric_value
            if self.numeric_minimum is None
            else min(self.numeric_minimum, numeric_value)
        )
        self.numeric_maximum = (
            numeric_value
            if self.numeric_maximum is None
            else max(self.numeric_maximum, numeric_value)
        )

    def result(self) -> tuple[HistoryIdOrdering | None, str | None, str | None]:
        if self.lexical_minimum is None or self.lexical_maximum is None:
            return None, None, None
        if self.all_numeric:
            if self.numeric_minimum is None or self.numeric_maximum is None:
                raise RuntimeError("numeric history-ID bounds were not recorded")
            return "numeric", self.numeric_minimum[1], self.numeric_maximum[1]
        return "lexical", self.lexical_minimum, self.lexical_maximum


@dataclass
class _PassState:
    started: float
    start: int
    page_filters: dict[str, JsonValue]
    calls: int = 0
    pages: int = 0
    raw_rows: int = 0
    unique_rows: int = 0
    same_rows: int = 0
    conflict_rows: int = 0
    declared_total: int | None = None
    total_observed: bool = False
    source_total_consistent: bool = True
    last_history_id: int | None = None
    operating_seconds: float = 0.0
    operating_samples: int = 0
    latest_operating_reset_at: float | None = None
    history_id_bounds: _HistoryIdBounds = dataclass_field(default_factory=_HistoryIdBounds)

    def observe_total(self, source_total: int | None) -> None:
        if not self.total_observed:
            self.declared_total = source_total
            self.total_observed = True
        elif source_total != self.declared_total:
            self.source_total_consistent = False

    def observe_disposition(self, disposition: str) -> None:
        if disposition == "unique":
            self.unique_rows += 1
        elif disposition == "same":
            self.same_rows += 1
        else:
            self.conflict_rows += 1


def collect_stage_history_pass(
    client: StageHistoryClient,
    *,
    source_contract_id: str,
    entity_type_id: int,
    filters: Mapping[str, JsonValue],
    limits: ProbeLimits,
    spool_directory: Path,
    pass_number: int,
    traversal_mode: TraversalMode = "offset",
    upper_history_id: int | None = None,
    owner_manifest_path: Path | None = None,
    owner_manifest_digest: str | None = None,
    redaction_key: RedactionKey | None = None,
    current_catalog_stage_keys: Collection[tuple[str, str]] | None = None,
) -> tuple[PassManifest, RestrictedSpool | CapabilityReconciliationSpool]:
    """Collect a bounded source pass using explicit offset or frozen keyset mode."""
    _validate_pass_inputs(traversal_mode, filters, entity_type_id, pass_number)
    if upper_history_id is not None and (
        isinstance(upper_history_id, bool) or upper_history_id < 0
    ):
        raise ValueError("upper_history_id must be non-negative")
    if (owner_manifest_path is None) != (owner_manifest_digest is None):
        raise ValueError("owner manifest path and digest must be supplied together")
    if owner_manifest_path is not None and traversal_mode != "id_keyset":
        raise ValueError("owner reconciliation requires frozen keyset traversal")
    normalized_contract_id = normalize_source_contract_id(source_contract_id)
    spool: RestrictedSpool | CapabilityReconciliationSpool
    spool = (
        CapabilityReconciliationSpool(spool_directory, pass_number)
        if owner_manifest_path is not None
        else RestrictedSpool(spool_directory, pass_number)
    )
    page_filters = dict(filters)
    if upper_history_id is not None:
        if "<=ID" in page_filters:
            raise ValueError("frozen traversal owns the <=ID filter")
        page_filters["<=ID"] = str(upper_history_id)
    state = _PassState(
        started=time.monotonic(),
        start=0 if traversal_mode == "offset" else -1,
        page_filters=page_filters,
    )
    try:
        _traverse_pages(
            client,
            source_contract_id=normalized_contract_id,
            entity_type_id=entity_type_id,
            limits=limits,
            spool=spool,
            state=state,
            traversal_mode=traversal_mode,
        )
        reconciliation = (
            spool.reconcile(
                owner_manifest_path,
                owner_manifest_digest,
                redaction_key=redaction_key,
                current_catalog_stage_keys=current_catalog_stage_keys,
            )
            if isinstance(spool, CapabilityReconciliationSpool)
            and owner_manifest_path is not None
            and owner_manifest_digest is not None
            else None
        )
        return _build_manifest(
            state,
            traversal_mode,
            limits,
            spool,
            upper_history_id,
            reconciliation,
            redaction_key,
        ), spool
    except BaseException:
        spool.delete()
        raise


def _validate_pass_inputs(
    traversal_mode: str,
    filters: Mapping[str, JsonValue],
    entity_type_id: int,
    pass_number: int,
) -> None:
    if traversal_mode not in {"offset", "id_keyset"}:
        raise ValueError("unsupported stage-history traversal mode")
    if traversal_mode == "id_keyset" and ">ID" in filters:
        raise ValueError("keyset traversal owns the >ID filter")
    if isinstance(entity_type_id, bool) or entity_type_id < 1:
        raise ValueError("entity_type_id must be positive")
    if isinstance(pass_number, bool) or pass_number < 1:
        raise ValueError("pass_number must be positive")


def _traverse_pages(
    client: StageHistoryClient,
    *,
    source_contract_id: str,
    entity_type_id: int,
    limits: ProbeLimits,
    spool: RestrictedSpool | CapabilityReconciliationSpool,
    state: _PassState,
    traversal_mode: TraversalMode,
) -> None:
    while True:
        if state.calls >= limits.max_calls:
            raise RuntimeError("Bitrix stage-history capability call limit exceeded")
        _check_state_limits(limits, state, spool.path)
        page = client.list_stage_history_page(
            entity_type_id=entity_type_id,
            filters=state.page_filters,
            order_direction="ASC",
            start=state.start,
        )
        state.calls += 1
        state.pages += 1
        if page.operating is not None:
            state.operating_seconds += page.operating
            state.operating_samples += 1
        if page.operating_reset_at is not None:
            state.latest_operating_reset_at = max(
                page.operating_reset_at,
                state.latest_operating_reset_at
                if state.latest_operating_reset_at is not None
                else page.operating_reset_at,
            )
        state.observe_total(page.total)
        _store_page_items(page, source_contract_id, limits, spool, state)
        if _page_completes_traversal(page, traversal_mode, state):
            return


def _store_page_items(
    page: StageHistoryPage,
    source_contract_id: str,
    limits: ProbeLimits,
    spool: RestrictedSpool | CapabilityReconciliationSpool,
    state: _PassState,
) -> None:
    for item in page.items:
        state.raw_rows += 1
        _check_state_limits(limits, state, spool.path)
        disposition = _store_item(spool, source_contract_id, item)
        state.history_id_bounds.add(item.history_id)
        state.observe_disposition(disposition)
        _check_state_limits(limits, state, spool.path)


def _store_item(
    spool: RestrictedSpool | CapabilityReconciliationSpool,
    source_contract_id: str,
    item: StageHistoryItem,
) -> str:
    stable_id = encode_stage_source_record_id(
        source_contract_id,
        item.entity_type_id,
        item.history_id,
    )
    canonical_hash = canonical_stage_hash_v1(source_contract_id, item)
    if isinstance(spool, CapabilityReconciliationSpool):
        return spool.add(
            stable_id=stable_id,
            canonical_hash=canonical_hash,
            owner_id=item.owner_id,
            category_id=item.category_id,
            stage_id=item.stage_id,
            event_at=item.created_time.isoformat(),
        )
    return spool.add(stable_id, canonical_hash)


def _page_completes_traversal(
    page: StageHistoryPage,
    traversal_mode: TraversalMode,
    state: _PassState,
) -> bool:
    if traversal_mode == "offset":
        if page.next_start is None:
            return True
        state.start = page.next_start
        return False
    page_ids = [_numeric_history_id(item.history_id) for item in page.items]
    if page_ids != sorted(page_ids) or len(set(page_ids)) != len(page_ids):
        raise RuntimeError("Bitrix stage-history keyset page was not strictly increasing")
    upper_id = state.page_filters.get("<=ID")
    if upper_id is not None and any(
        value > _numeric_history_id(str(upper_id)) for value in page_ids
    ):
        raise RuntimeError("Bitrix stage-history keyset exceeded its frozen upper boundary")
    if page_ids and state.last_history_id is not None and page_ids[0] <= state.last_history_id:
        raise RuntimeError("Bitrix stage-history keyset did not advance")
    if len(page.items) < _BITRIX_PAGE_SIZE:
        return True
    if not page_ids:
        raise RuntimeError("Bitrix stage-history keyset returned an invalid full page")
    state.last_history_id = page_ids[-1]
    state.page_filters[">ID"] = str(state.last_history_id)
    return False


def _build_manifest(
    state: _PassState,
    traversal_mode: TraversalMode,
    limits: ProbeLimits,
    spool: RestrictedSpool | CapabilityReconciliationSpool,
    upper_history_id: int | None,
    reconciliation: ReconciliationSummary | None,
    redaction_key: RedactionKey | None,
) -> PassManifest:
    spool.flush()
    _check_state_limits(limits, state, spool.path)
    ordering, minimum_id, maximum_id = state.history_id_bounds.result()
    total_matches_rows = _source_total_matches_rows(state, traversal_mode)
    identity_hash_digest = (
        spool.manifest_digest(redaction_key=redaction_key)
        if isinstance(spool, CapabilityReconciliationSpool)
        else digest_value(
            spool.manifest_digest(),
            domain="bitrix-capability-stage-legacy-identity-hash-v1",
            redaction_key=redaction_key,
        )
    )
    _check_state_limits(limits, state, spool.path)
    return PassManifest(
        traversal_mode=traversal_mode,
        calls=state.calls,
        raw_rows=state.raw_rows,
        unique_identity_rows=state.unique_rows,
        duplicate_same_hash_rows=state.same_rows,
        duplicate_conflict_rows=state.conflict_rows,
        pages=state.pages,
        source_total=state.declared_total,
        source_total_consistent=state.source_total_consistent,
        source_total_matches_rows=total_matches_rows,
        history_id_ordering=ordering,
        minimum_history_id=minimum_id,
        maximum_history_id=maximum_id,
        identity_hash_digest=identity_hash_digest,
        runtime_seconds=time.monotonic() - state.started,
        spool_bytes=spool_storage_bytes(spool.path),
        upper_history_id_digest=(
            digest_value(
                upper_history_id,
                domain="bitrix-capability-stage-history-upper-id-v1",
                redaction_key=redaction_key,
            )
            if upper_history_id is not None
            else None
        ),
        owner_manifest_digest=(reconciliation.owner_manifest_digest if reconciliation else None),
        global_rows=(reconciliation.global_rows if reconciliation else None),
        in_scope_rows=(reconciliation.in_scope_rows if reconciliation else None),
        out_of_scope_rows=(reconciliation.out_of_scope_rows if reconciliation else None),
        owners_without_history=(reconciliation.owners_without_history if reconciliation else None),
        in_scope_identity_hash_digest=(
            reconciliation.in_scope_identity_hash_digest if reconciliation else None
        ),
        category_inventory_digest=(
            reconciliation.category_inventory_digest if reconciliation else None
        ),
        stage_inventory_digest=(reconciliation.stage_inventory_digest if reconciliation else None),
        equal_time_group_digest=(
            reconciliation.equal_time_group_digest if reconciliation else None
        ),
        operating_seconds=state.operating_seconds,
        operating_samples=state.operating_samples,
        latest_operating_reset_at=state.latest_operating_reset_at,
        current_catalog_stage_count=(
            reconciliation.current_catalog_stage_count if reconciliation else None
        ),
        in_scope_historical_stage_count=(
            reconciliation.in_scope_historical_stage_count if reconciliation else None
        ),
        in_scope_historical_stage_missing_catalog_count=(
            reconciliation.in_scope_historical_stage_missing_catalog_count
            if reconciliation
            else None
        ),
        in_scope_rows_missing_stage_identity=(
            reconciliation.in_scope_rows_missing_stage_identity if reconciliation else None
        ),
    )


def _source_total_matches_rows(
    state: _PassState,
    traversal_mode: TraversalMode,
) -> bool | None:
    if (
        traversal_mode != "offset"
        or not state.source_total_consistent
        or state.declared_total is None
    ):
        return None
    return state.declared_total == state.raw_rows


def _check_state_limits(limits: ProbeLimits, state: _PassState, spool_path: Path) -> None:
    _check_limits(
        limits,
        started=state.started,
        calls=state.calls,
        raw_rows=state.raw_rows,
        spool_path=spool_path,
    )


def manifests_are_identical(first: PassManifest, second: PassManifest) -> bool:
    """Compare source evidence, excluding measured runtime and spool file size."""
    return (
        first.traversal_mode == second.traversal_mode
        and first.calls == second.calls
        and first.pages == second.pages
        and first.raw_rows == second.raw_rows
        and first.unique_identity_rows == second.unique_identity_rows
        and first.duplicate_same_hash_rows == second.duplicate_same_hash_rows
        and first.duplicate_conflict_rows == second.duplicate_conflict_rows
        and first.source_total == second.source_total
        and first.source_total_consistent == second.source_total_consistent
        and first.source_total_consistent
        and first.source_total_matches_rows == second.source_total_matches_rows
        and first.source_total_matches_rows is not False
        and first.history_id_ordering == second.history_id_ordering
        and first.minimum_history_id == second.minimum_history_id
        and first.maximum_history_id == second.maximum_history_id
        and first.identity_hash_digest == second.identity_hash_digest
        and first.upper_history_id_digest == second.upper_history_id_digest
        and first.owner_manifest_digest == second.owner_manifest_digest
        and first.global_rows == second.global_rows
        and first.in_scope_rows == second.in_scope_rows
        and first.out_of_scope_rows == second.out_of_scope_rows
        and first.owners_without_history == second.owners_without_history
        and first.in_scope_identity_hash_digest == second.in_scope_identity_hash_digest
        and first.category_inventory_digest == second.category_inventory_digest
        and first.stage_inventory_digest == second.stage_inventory_digest
        and first.equal_time_group_digest == second.equal_time_group_digest
        and first.current_catalog_stage_count == second.current_catalog_stage_count
        and first.in_scope_historical_stage_count == second.in_scope_historical_stage_count
        and first.in_scope_historical_stage_missing_catalog_count
        == second.in_scope_historical_stage_missing_catalog_count
        and first.in_scope_rows_missing_stage_identity
        == second.in_scope_rows_missing_stage_identity
    )


def _numeric_history_id(value: str) -> int:
    if not value.isdigit():
        raise RuntimeError("Bitrix stage-history keyset requires numeric IDs")
    return int(value)


def _check_limits(
    limits: ProbeLimits,
    *,
    started: float,
    calls: int,
    raw_rows: int,
    spool_path: Path,
) -> None:
    if calls > limits.max_calls:
        raise RuntimeError("Bitrix stage-history capability call limit exceeded")
    if raw_rows > limits.max_rows:
        raise RuntimeError("Bitrix stage-history capability row limit exceeded")
    if time.monotonic() - started > limits.max_runtime_seconds:
        raise RuntimeError("Bitrix stage-history capability runtime limit exceeded")
    if spool_storage_bytes(spool_path) > limits.max_spool_bytes:
        raise RuntimeError("Bitrix stage-history capability spool limit exceeded")


def freeze_stage_history_upper_id(client: StageHistoryClient, entity_type_id: int) -> int:
    """Freeze a global numeric stage-history upper boundary without source filtering."""
    page = client.list_stage_history_page(
        entity_type_id=entity_type_id, filters={}, order_direction="DESC", start=-1
    )
    if len(page.items) > _BITRIX_PAGE_SIZE:
        raise RuntimeError("Bitrix stage-history boundary probe exceeded the fixed page size")
    if not page.items:
        return 0
    ids = [_numeric_history_id(item.history_id) for item in page.items]
    if ids != sorted(ids, reverse=True) or len(ids) != len(set(ids)):
        raise RuntimeError("Bitrix stage-history boundary probe was not descending")
    return ids[0]
