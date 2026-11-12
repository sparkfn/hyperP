"""Async snapshot and net-delta primitives for the maintained Person-list summary.

The maintained counter is kept in step by the writers that touch the graph. A
writer captures the summary contributions of the nodes it is about to change,
mutates them, captures the contributions again, and applies the net difference
to the counter in a single counter query.

The snapshot is deliberately narrow: only the affected node ids are read, and
only the four published Person flags and the distinct-deal qualification are
captured. The counter query decides whether a delta may be applied: the
counter's readiness, the validity of the stored values and the published
month marker all gate the update, so a writer can never publish a value the
counter cannot vouch for.

This module mirrors ``services/ingestion/src/graph/person_list_summary_deltas.py``
so both services keep the same semantics; the two services stay independent
(no cross-service imports), following the existing duplicated CRM-count
query-module precedent. The API side has no drift accounting and no batch
capture: it only owns the async snapshot/delta primitives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from neo4j import AsyncManagedTransaction, Record

from src.graph.queries.person_list_summary import (
    APPLY_PERSON_LIST_SUMMARY_DELTAS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS,
    CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
    CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS,
    FIND_DEAL_ELEMENT_IDS_FOR_PERSONS,
    PERSON_LIST_SUMMARY_COUNTER_KEY,
)

#: Key prefix of a source-record-pk selector that resolved to no node.
MISSING_PK_KEY_PREFIX = "missing:pk"

#: Key prefix of an element-id selector that resolved to no node.
MISSING_ELEMENT_ID_KEY_PREFIX = "missing:element"


@dataclass(frozen=True)
class PersonContribution:
    """The four published Person-derived flags of one Person node.

    A missing node contributes the same as a node that fails every predicate:
    all four flags are false.
    """

    qualifying_status: bool
    high_risk: bool
    high_value: bool
    no_contact: bool


@dataclass(frozen=True)
class DealContribution:
    """The distinct-deal contribution of one SourceRecord node.

    ``observed_at`` is carried only by a qualifying deal.
    """

    qualifying: bool
    observed_at: str | None


@dataclass(frozen=True)
class PersonListSummarySnapshot:
    """The summary contributions of the nodes a writer touched."""

    persons: Mapping[str, PersonContribution]
    deals: Mapping[str, DealContribution]


@dataclass(frozen=True)
class PersonListSummaryDeltaPlan:
    """The net contribution difference of one writer transaction.

    The month-scoped contribution is not a number here: the month boundary is
    sampled inside the counter query, after the counter's lock is taken, so
    the qualifying observed_at strings are carried instead.
    """

    active_person_count: int
    high_risk_count: int
    high_value_count: int
    no_contact_count: int
    all_deals_count: int
    before_observed_at: tuple[str, ...]
    after_observed_at: tuple[str, ...]


@dataclass(frozen=True)
class PersonListSummaryDeltaApplication:
    """The counter state after a delta query ran.

    The counts are ``None`` when the stored value is not a usable integer:
    a malformed counter is reported, never repaired by a writer.
    """

    ready: bool
    core_applied: bool
    all_deals_applied: bool
    monthly_applied: bool
    current_month: str
    active_person_count: int | None
    high_risk_count: int | None
    high_value_count: int | None
    no_contact_count: int | None
    all_deals_count: int | None
    deals_this_month_count: int | None


_ZERO_PERSON = PersonContribution(
    qualifying_status=False,
    high_risk=False,
    high_value=False,
    no_contact=False,
)

_ZERO_DEAL = DealContribution(qualifying=False, observed_at=None)


async def find_deal_element_ids_for_persons(
    tx: AsyncManagedTransaction,
    person_ids: Sequence[str],
) -> tuple[str, ...]:
    """Return the element ids of the distinct deals linked to the given Persons.

    The selection is bounded by the requested person ids and returns one
    element id per distinct deal. Only CRM deals behind an active-or-legacy-
    active link are returned: those are the only records whose distinct-deal
    contribution can change.
    """
    ids = sorted({person_id for person_id in person_ids if person_id})
    if not ids:
        return ()
    element_ids: list[str] = []
    result = await tx.run(FIND_DEAL_ELEMENT_IDS_FOR_PERSONS, person_ids=ids)
    async for record in result:
        element_id = _field(record, "deal_element_id")
        if isinstance(element_id, str) and element_id and element_id not in element_ids:
            element_ids.append(element_id)
    return tuple(element_ids)


async def capture(
    tx: AsyncManagedTransaction,
    person_ids: Sequence[str],
    source_record_pks: Sequence[str],
    deal_element_ids: Sequence[str] = (),
) -> PersonListSummarySnapshot:
    """Capture the summary contributions of the given node ids.

    A node that does not exist contributes nothing: its entry carries the
    same false flags as a node that fails every predicate.

    Deals may be addressed by source-record pk or by element id, because a
    distinct deal can carry no source-record pk at all. Both selectors address
    the same node space, and a resolved deal is keyed by its element id so it
    keeps one key whichever selector found it.
    """
    persons = await _capture_persons(tx, person_ids)
    deals = await _capture_deals(tx, source_record_pks, deal_element_ids)
    return PersonListSummarySnapshot(persons=persons, deals=deals)


async def apply(
    tx: AsyncManagedTransaction,
    before: PersonListSummarySnapshot,
    after: PersonListSummarySnapshot,
) -> PersonListSummaryDeltaApplication:
    """Apply the snapshot difference to the maintained counter.

    Exactly one counter query runs, even when every delta is zero: the
    counter's lock is taken and its state is reported back, so a writer can
    tell that the counter was reached at all.
    """
    plan = plan_deltas(before, after)
    result = await tx.run(
        APPLY_PERSON_LIST_SUMMARY_DELTAS,
        key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        active_person_delta=plan.active_person_count,
        high_risk_delta=plan.high_risk_count,
        high_value_delta=plan.high_value_count,
        no_contact_delta=plan.no_contact_count,
        all_deals_delta=plan.all_deals_count,
        before_observed_at=list(plan.before_observed_at),
        after_observed_at=list(plan.after_observed_at),
    )
    record = await result.single()
    if record is None:
        raise RuntimeError("person-list summary counter is missing; cannot apply summary deltas")
    return _parse_application(record)


def plan_deltas(
    before: PersonListSummarySnapshot,
    after: PersonListSummarySnapshot,
) -> PersonListSummaryDeltaPlan:
    """Compute the net contribution difference of two snapshots.

    Every delta is computed once, over the union of the captured ids, so an
    id that only exists in one of the two snapshots contributes its full
    value.
    """
    active_delta = 0
    high_risk_delta = 0
    high_value_delta = 0
    no_contact_delta = 0
    for person_id in before.persons.keys() | after.persons.keys():
        before_person = before.persons.get(person_id)
        after_person = after.persons.get(person_id)
        before_flags = _ZERO_PERSON if before_person is None else before_person
        after_flags = _ZERO_PERSON if after_person is None else after_person
        active_delta += int(after_flags.qualifying_status) - int(before_flags.qualifying_status)
        high_risk_delta += int(after_flags.high_risk) - int(before_flags.high_risk)
        high_value_delta += int(after_flags.high_value) - int(before_flags.high_value)
        no_contact_delta += int(after_flags.no_contact) - int(before_flags.no_contact)

    all_deals_delta = 0
    before_observed: list[str] = []
    after_observed: list[str] = []
    for deal_key in before.deals.keys() | after.deals.keys():
        before_deal = before.deals.get(deal_key)
        after_deal = after.deals.get(deal_key)
        before_deal_flags = _ZERO_DEAL if before_deal is None else before_deal
        after_deal_flags = _ZERO_DEAL if after_deal is None else after_deal
        all_deals_delta += int(after_deal_flags.qualifying) - int(before_deal_flags.qualifying)
        if before_deal_flags.qualifying and before_deal_flags.observed_at is not None:
            before_observed.append(before_deal_flags.observed_at)
        if after_deal_flags.qualifying and after_deal_flags.observed_at is not None:
            after_observed.append(after_deal_flags.observed_at)

    return PersonListSummaryDeltaPlan(
        active_person_count=active_delta,
        high_risk_count=high_risk_delta,
        high_value_count=high_value_delta,
        no_contact_count=no_contact_delta,
        all_deals_count=all_deals_delta,
        before_observed_at=tuple(before_observed),
        after_observed_at=tuple(after_observed),
    )


class PersonListSummaryDeltaCollector:
    """Collects the summary contributions of the nodes one transaction touches.

    Nested writers share one collector. Each writer captures the nodes it is
    about to change before it changes them, and the collector keeps the first,
    pre-transaction snapshot of every node: a nested writer can neither re-read
    nor replace that snapshot with the already-mutated state.

    The collector is closed by :meth:`finalize`, which captures the after state
    of every tracked node and applies the net difference to the counter exactly
    once.

    The state is transaction-local: the collector is created per transaction
    and passed explicitly to every writer that takes part.
    """

    def __init__(self) -> None:
        self._tracked_person_ids: set[str] = set()
        self._tracked_source_record_pks: set[str] = set()
        self._tracked_deal_element_ids: set[str] = set()
        self._before_persons: dict[str, PersonContribution] = {}
        self._before_deals: dict[str, DealContribution] = {}
        self._finalized = False

    async def capture_before(
        self,
        tx: AsyncManagedTransaction,
        person_ids: Sequence[str] = (),
        source_record_pks: Sequence[str] = (),
        deal_element_ids: Sequence[str] = (),
    ) -> None:
        """Capture the pre-transaction contribution of the given nodes.

        Only ids that are not known yet are read, and the first snapshot of a
        node is preserved: a nested writer can therefore neither re-read nor
        overwrite the pre-transaction state of a node another writer already
        captured. The source-record-pk and element-id selectors are tracked
        separately, because either of them can address the same node.
        """
        self._ensure_open()
        new_person_ids = self._unknown(person_ids, self._tracked_person_ids)
        new_deal_pks = self._unknown(source_record_pks, self._tracked_source_record_pks)
        new_deal_element_ids = self._unknown(deal_element_ids, self._tracked_deal_element_ids)
        self._tracked_person_ids.update(identifier for identifier in person_ids if identifier)
        self._tracked_source_record_pks.update(
            identifier for identifier in source_record_pks if identifier
        )
        self._tracked_deal_element_ids.update(
            identifier for identifier in deal_element_ids if identifier
        )
        snapshot = await capture(tx, new_person_ids, new_deal_pks, new_deal_element_ids)
        for person_id, contribution in snapshot.persons.items():
            self._before_persons.setdefault(person_id, contribution)
        for deal_key, contribution in snapshot.deals.items():
            self._before_deals.setdefault(deal_key, contribution)

    def mark_new(
        self,
        person_ids: Sequence[str] = (),
        source_record_pks: Sequence[str] = (),
        deal_element_ids: Sequence[str] = (),
    ) -> None:
        """Record nodes created by this transaction as having no prior state.

        A node created inside the transaction has no pre-transaction state: its
        before contribution is an explicit zero, so a nested writer that
        captures the same node later cannot mistake the created node for
        pre-existing state.
        """
        self._ensure_open()
        for person_id in person_ids:
            if not person_id:
                continue
            self._tracked_person_ids.add(person_id)
            self._before_persons.setdefault(person_id, _ZERO_PERSON)
        for source_record_pk in source_record_pks:
            if not source_record_pk:
                continue
            self._tracked_source_record_pks.add(source_record_pk)
            self._before_deals.setdefault(
                _deal_snapshot_key(
                    source_record_pk,
                    None,
                    missing_prefix=MISSING_PK_KEY_PREFIX,
                ),
                _ZERO_DEAL,
            )
        for deal_element_id in deal_element_ids:
            if not deal_element_id:
                continue
            self._tracked_deal_element_ids.add(deal_element_id)
            self._before_deals.setdefault(
                _deal_snapshot_key(
                    deal_element_id,
                    None,
                    missing_prefix=MISSING_ELEMENT_ID_KEY_PREFIX,
                ),
                _ZERO_DEAL,
            )

    async def finalize(
        self,
        tx: AsyncManagedTransaction,
        deal_element_ids: Sequence[str] = (),
    ) -> PersonListSummaryDeltaApplication:
        """Capture the after state of every tracked node and apply the delta.

        Exactly one delta query runs, even when every delta is zero. The
        collector is closed afterwards, so a second finalize is rejected.
        """
        self._ensure_open()
        if deal_element_ids:
            self.mark_new(deal_element_ids=deal_element_ids)
        self._finalized = True
        after = await capture(
            tx,
            sorted(self._tracked_person_ids),
            sorted(self._tracked_source_record_pks),
            sorted(self._tracked_deal_element_ids),
        )
        before = PersonListSummarySnapshot(
            persons=dict(self._before_persons),
            deals=dict(self._before_deals),
        )
        return await apply(tx, before, after)

    @staticmethod
    def _unknown(ids: Sequence[str], known: set[str]) -> list[str]:
        """Return the non-empty ids that are not known yet."""
        return [identifier for identifier in ids if identifier and identifier not in known]

    def _ensure_open(self) -> None:
        """Reject any use of a collector that was already finalized."""
        if self._finalized:
            raise RuntimeError("person-list summary delta collector is already finalized")


async def _capture_persons(
    tx: AsyncManagedTransaction,
    person_ids: Sequence[str],
) -> dict[str, PersonContribution]:
    """Capture the four published Person flags of the requested ids."""
    ids = sorted({person_id for person_id in person_ids if person_id})
    if not ids:
        return {}
    contributions: dict[str, PersonContribution] = {}
    result = await tx.run(CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS, person_ids=ids)
    async for record in result:
        contributions[str(_field(record, "person_id"))] = PersonContribution(
            qualifying_status=_bool_field(record, "qualifying_status"),
            high_risk=_bool_field(record, "high_risk"),
            high_value=_bool_field(record, "high_value"),
            no_contact=_bool_field(record, "no_contact"),
        )
    return contributions


async def _capture_deals(
    tx: AsyncManagedTransaction,
    source_record_pks: Sequence[str],
    deal_element_ids: Sequence[str],
) -> dict[str, DealContribution]:
    """Capture the distinct-deal contribution of the requested selectors.

    The pk selectors are captured first: a deal addressed by both kinds of
    selector keeps the first captured contribution.
    """
    contributions = await _capture_deals_by_source_record_pk(tx, source_record_pks)
    for key, contribution in (await _capture_deals_by_element_id(tx, deal_element_ids)).items():
        contributions.setdefault(key, contribution)
    return contributions


async def _capture_deals_by_source_record_pk(
    tx: AsyncManagedTransaction,
    source_record_pks: Sequence[str],
) -> dict[str, DealContribution]:
    """Capture the distinct-deal contribution of the requested pk selectors."""
    pks = sorted({pk for pk in source_record_pks if pk})
    if not pks:
        return {}
    contributions: dict[str, DealContribution] = {}
    result = await tx.run(CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS, source_record_pks=pks)
    async for record in result:
        selector = str(_field(record, "source_record_pk"))
        key = _deal_snapshot_key(
            selector,
            _field(record, "resolved_element_id"),
            missing_prefix=MISSING_PK_KEY_PREFIX,
        )
        contributions[key] = DealContribution(
            qualifying=_bool_field(record, "qualifying"),
            observed_at=_optional_string_field(record, "observed_at"),
        )
    return contributions


async def _capture_deals_by_element_id(
    tx: AsyncManagedTransaction,
    deal_element_ids: Sequence[str],
) -> dict[str, DealContribution]:
    """Capture the distinct-deal contribution of the requested element ids."""
    element_ids = sorted({element_id for element_id in deal_element_ids if element_id})
    if not element_ids:
        return {}
    contributions: dict[str, DealContribution] = {}
    result = await tx.run(
        CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID,
        deal_element_ids=element_ids,
    )
    async for record in result:
        selector = str(_field(record, "deal_element_id"))
        key = _deal_snapshot_key(
            selector,
            _field(record, "resolved_element_id"),
            missing_prefix=MISSING_ELEMENT_ID_KEY_PREFIX,
        )
        contributions[key] = DealContribution(
            qualifying=_bool_field(record, "qualifying"),
            observed_at=_optional_string_field(record, "observed_at"),
        )
    return contributions


def _deal_snapshot_key(
    selector: str,
    resolved_element_id: object | None,
    *,
    missing_prefix: str,
) -> str:
    """Return the snapshot key of one addressed deal.

    A resolved deal is keyed by its element id, so the same node keeps one key
    whichever selector found it. A selector that resolved to no node keeps a
    deterministic missing key.
    """
    if isinstance(resolved_element_id, str) and resolved_element_id:
        return resolved_element_id
    return f"{missing_prefix}:{selector}"


def _parse_application(record: Record) -> PersonListSummaryDeltaApplication:
    """Build the typed application result from a counter row."""
    return PersonListSummaryDeltaApplication(
        ready=_bool_field(record, "ready"),
        core_applied=_bool_field(record, "core_applied"),
        all_deals_applied=_bool_field(record, "all_deals_applied"),
        monthly_applied=_bool_field(record, "monthly_applied"),
        current_month=_string_field(record, "current_month_key"),
        active_person_count=_count_field(record, "active_person_count"),
        high_risk_count=_count_field(record, "high_risk_count"),
        high_value_count=_count_field(record, "high_value_count"),
        no_contact_count=_count_field(record, "no_contact_count"),
        all_deals_count=_count_field(record, "all_deals_count"),
        deals_this_month_count=_count_field(record, "deals_this_month_count"),
    )


def _field(record: Record, name: str) -> object:
    """Return one raw field of a Neo4j record as a plain object."""
    value: object = record[name]
    return value


def _bool_field(record: Record, name: str) -> bool:
    """Return a captured boolean field, rejecting a non-boolean value."""
    value = _field(record, name)
    if not isinstance(value, bool):
        raise RuntimeError(f"person-list summary returned a non-boolean {name}")
    return value


def _string_field(record: Record, name: str) -> str:
    """Return a captured string field, rejecting a non-string value."""
    value = _field(record, name)
    if not isinstance(value, str):
        raise RuntimeError(f"person-list summary returned a non-string {name}")
    return value


def _optional_string_field(record: Record, name: str) -> str | None:
    """Return a captured string field, or None when the value is null."""
    value = _field(record, name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"person-list summary returned a non-string {name}")
    return value


def _count_field(record: Record, name: str) -> int | None:
    """Return a stored count, or None when the stored value is not an integer."""
    value = _field(record, name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
