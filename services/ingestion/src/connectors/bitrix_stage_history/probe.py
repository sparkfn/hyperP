"""Bounded, read-only traversal evidence for Bitrix stage history."""

from __future__ import annotations

import time
from collections.abc import Mapping
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
) -> tuple[PassManifest, RestrictedSpool]:
    """Collect a bounded source pass using explicit offset or candidate keyset mode."""
    _validate_pass_inputs(traversal_mode, filters, entity_type_id, pass_number)
    normalized_contract_id = normalize_source_contract_id(source_contract_id)
    spool = RestrictedSpool(spool_directory, pass_number)
    state = _PassState(
        started=time.monotonic(),
        start=0 if traversal_mode == "offset" else -1,
        page_filters=dict(filters),
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
        return _build_manifest(state, traversal_mode, limits, spool), spool
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
    spool: RestrictedSpool,
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
        state.observe_total(page.total)
        _store_page_items(page, source_contract_id, limits, spool, state)
        if _page_completes_traversal(page, traversal_mode, state):
            return


def _store_page_items(
    page: StageHistoryPage,
    source_contract_id: str,
    limits: ProbeLimits,
    spool: RestrictedSpool,
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
    spool: RestrictedSpool,
    source_contract_id: str,
    item: StageHistoryItem,
) -> str:
    stable_id = encode_stage_source_record_id(
        source_contract_id,
        item.entity_type_id,
        item.history_id,
    )
    canonical_hash = canonical_stage_hash_v1(source_contract_id, item)
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
    spool: RestrictedSpool,
) -> PassManifest:
    spool.flush()
    _check_state_limits(limits, state, spool.path)
    ordering, minimum_id, maximum_id = state.history_id_bounds.result()
    total_matches_rows = _source_total_matches_rows(state, traversal_mode)
    identity_hash_digest = spool.manifest_digest()
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
