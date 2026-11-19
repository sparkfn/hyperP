"""Maintenance of the maintained Person-list summary counter (issue #489).

The person-list summary is served from a single maintained counter node,
``(:PersonListSummaryCounter {key: 'global'})``. This module owns the two
mutating operations on that node:

* recompute and publish (explicit operator backfill, and the nightly
  reconciliation), and
* invalidation (used when the graph is reset underneath the counter).

Every recompute attempt runs as exactly one Neo4j write transaction. The
transaction takes the counter node's write lock, samples the database month
boundary once after that lock, computes the authoritative aggregates, and
publishes the six values, the month marker and the readiness flag together.
If the database month moves before the commit, nothing is published: the
attempt is rolled back and retried once against a fresh month.

Publication is never implicit. The nightly reconciliation leaves an unready
counter unready; only the explicit backfill activates it.

This module also owns the drift accounting: the absolute drift of every
published field against the value that was stored before the publication, and
the threshold that turns such a drift into an alarm.
"""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction, Record
from pydantic.types import JsonValue

from src.graph.client import Neo4jClient
from src.graph.queries.person_list_summary import (
    GET_PERSON_LIST_SUMMARY_PUBLICATION_STATE,
    INVALIDATE_PERSON_LIST_SUMMARY,
    PERSON_LIST_SUMMARY_COUNTER_KEY,
    RECOMPUTE_PERSON_LIST_SUMMARY,
)

#: Number of recompute attempts: the first attempt plus one retry against a
#: fresh database month boundary.
MAX_RECOMPUTE_ATTEMPTS = 2

#: The month-scoped published field. Its value legitimately changes whenever
#: the published month marker rolls over.
PERSON_LIST_SUMMARY_MONTH_SCOPED_FIELD = "deals_this_month_count"


class _RecomputeAbortedError(RuntimeError):
    """Internal signal that a recompute attempt published no row.

    The signal is raised inside the write transaction so the transaction is
    rolled back, undoing the counter's ``updated_at`` touch.
    """


@dataclass(frozen=True)
class PersonListSummaryCounts:
    """The six published summary values."""

    active_person_count: int
    high_risk_count: int
    high_value_count: int
    no_contact_count: int
    deals_this_month_count: int
    all_deals_count: int


@dataclass(frozen=True)
class PersonListSummaryPreviousState:
    """Counter values as they were stored before the publication.

    The previous values are reported for drift accounting only. They may be
    missing or malformed, so they are kept as raw JSON values instead of being
    coerced.
    """

    active_person_count: JsonValue
    high_risk_count: JsonValue
    high_value_count: JsonValue
    no_contact_count: JsonValue
    deals_this_month_count: JsonValue
    all_deals_count: JsonValue
    month: JsonValue
    ready: JsonValue


@dataclass(frozen=True)
class PersonListSummaryPublication:
    """Result of one successful recompute and publication."""

    previous: PersonListSummaryPreviousState
    new: PersonListSummaryCounts
    new_month: str
    activated: bool


@dataclass(frozen=True)
class PersonListSummaryInvalidation:
    """Result of invalidating the counter."""

    ready: bool
    generation: int


@dataclass(frozen=True)
class PersonListSummaryPublicationState:
    """Read-only view of the counter's publication state."""

    ready: JsonValue
    month: JsonValue
    generation: JsonValue


def backfill_person_list_summary(client: Neo4jClient) -> PersonListSummaryPublication:
    """Recompute and publish the counter, activating it.

    This is the explicit operator backfill: it publishes the six values even
    when the counter is currently unready, and it marks the counter ready.
    """
    publication = recompute_person_list_summary(client, activate=True)
    if publication is None:
        raise RuntimeError("person-list summary backfill published nothing")
    return publication


def reconcile_person_list_summary(
    client: Neo4jClient,
) -> PersonListSummaryPublication | None:
    """Recompute and publish the counter without activating it.

    Returns ``None`` when the counter is not ready and therefore has nothing to
    reconcile: an unready counter stays unready.
    """
    return recompute_person_list_summary(client, activate=False)


def recompute_person_list_summary(
    client: Neo4jClient, *, activate: bool
) -> PersonListSummaryPublication | None:
    """Recompute and publish the six summary values in one write transaction.

    Returns the publication, or ``None`` when the counter is not ready and
    ``activate`` is false (nothing to reconcile).
    """
    for attempt in range(1, MAX_RECOMPUTE_ATTEMPTS + 1):
        try:
            return _recompute_once(client, activate=activate)
        except _RecomputeAbortedError:
            state = _read_publication_state(client)
            if state is None:
                raise RuntimeError(
                    "person-list summary counter is missing; cannot recompute"
                ) from None
            if not isinstance(state.ready, bool):
                raise RuntimeError(
                    "person-list summary counter readiness is not a boolean"
                ) from None
            if not state.ready and not activate:
                return None
            if attempt >= MAX_RECOMPUTE_ATTEMPTS:
                raise RuntimeError(
                    "person-list summary recompute crossed the database month "
                    f"boundary {attempt} times without publishing"
                ) from None
    raise RuntimeError("person-list summary recompute did not complete")


def invalidate_person_list_summary(client: Neo4jClient) -> PersonListSummaryInvalidation:
    """Mark the counter unready and advance its generation.

    The stored counts and month marker are preserved so a later backfill can
    report the drift it repaired.
    """

    def _work(tx: ManagedTransaction) -> PersonListSummaryInvalidation:
        record = tx.run(
            INVALIDATE_PERSON_LIST_SUMMARY,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).single()
        if record is None:
            raise RuntimeError("person-list summary counter is missing; cannot invalidate")
        return PersonListSummaryInvalidation(
            ready=_require_bool(_field(record, "ready"), field="ready"),
            generation=_require_non_negative_int(_field(record, "generation"), field="generation"),
        )

    return client.execute_write(_work)


@dataclass(frozen=True)
class PersonListSummaryFieldDrift:
    """Absolute drift of one published summary field.

    ``drift`` is ``None`` when the stored previous value is not a usable
    integer: the drift of that field is then unknown rather than zero.
    """

    field: str
    previous: JsonValue
    current: int
    valid_previous: bool
    drift: int | None


def person_list_summary_field_drifts(
    publication: PersonListSummaryPublication,
) -> tuple[PersonListSummaryFieldDrift, ...]:
    """Return the per-field absolute drift of a publication."""
    previous = publication.previous
    current = publication.new
    return (
        _field_drift(
            "active_person_count", previous.active_person_count, current.active_person_count
        ),
        _field_drift("high_risk_count", previous.high_risk_count, current.high_risk_count),
        _field_drift("high_value_count", previous.high_value_count, current.high_value_count),
        _field_drift("no_contact_count", previous.no_contact_count, current.no_contact_count),
        _field_drift(
            PERSON_LIST_SUMMARY_MONTH_SCOPED_FIELD,
            previous.deals_this_month_count,
            current.deals_this_month_count,
        ),
        _field_drift("all_deals_count", previous.all_deals_count, current.all_deals_count),
    )


def person_list_summary_month_rolled_over(
    publication: PersonListSummaryPublication,
) -> bool:
    """Return whether the published month marker moved forward by a rollover.

    A rollover is the only intentional reason for the month-scoped deal count
    to change: the stored marker belongs to an earlier month. A missing or
    malformed marker is not a rollover, so the month-scoped drift of such a
    counter stays subject to the drift threshold.
    """
    previous = publication.previous.month
    if not isinstance(previous, str):
        return False
    if not _is_month_marker(previous):
        return False
    return previous < publication.new_month


def exceeds_person_list_summary_drift_threshold(drift: int, denominator: int) -> bool:
    """Return whether an absolute drift breaches the alarm threshold.

    The threshold is one per mille of the authoritative Person count. A zero
    denominator (an empty graph) requires exactly zero drift.
    """
    if denominator <= 0:
        return drift > 0
    return drift * 1000 >= denominator


def person_list_summary_alarming_fields(
    drifts: tuple[PersonListSummaryFieldDrift, ...],
    *,
    denominator: int,
    excluded_fields: frozenset[str] = frozenset(),
) -> tuple[PersonListSummaryFieldDrift, ...]:
    """Return the fields whose absolute drift breaches the alarm threshold.

    A field whose previous value was not a usable integer has an unknown
    drift and therefore cannot breach the threshold; it is reported as
    invalid instead.
    """
    return tuple(
        drift
        for drift in drifts
        if drift.field not in excluded_fields
        and drift.drift is not None
        and exceeds_person_list_summary_drift_threshold(drift.drift, denominator)
    )


def _recompute_once(client: Neo4jClient, *, activate: bool) -> PersonListSummaryPublication:
    """Run one recompute attempt in a single write transaction."""

    def _work(tx: ManagedTransaction) -> PersonListSummaryPublication:
        record = tx.run(
            RECOMPUTE_PERSON_LIST_SUMMARY,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
            activate=activate,
        ).single()
        if record is None:
            raise _RecomputeAbortedError("person-list summary recompute published no row")
        return _parse_publication(record)

    return client.execute_write(_work)


def _read_publication_state(
    client: Neo4jClient,
) -> PersonListSummaryPublicationState | None:
    """Read the counter's publication state outside the aborted transaction."""

    def _work(tx: ManagedTransaction) -> PersonListSummaryPublicationState | None:
        record = tx.run(
            GET_PERSON_LIST_SUMMARY_PUBLICATION_STATE,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        ).single()
        if record is None:
            return None
        return PersonListSummaryPublicationState(
            ready=_to_json_value(_field(record, "ready")),
            month=_to_json_value(_field(record, "month")),
            generation=_to_json_value(_field(record, "generation")),
        )

    return client.execute_read(_work)


def _parse_publication(record: Record) -> PersonListSummaryPublication:
    """Build the typed publication result from a recompute row."""
    previous = PersonListSummaryPreviousState(
        active_person_count=_to_json_value(_field(record, "previous_active_person_count")),
        high_risk_count=_to_json_value(_field(record, "previous_high_risk_count")),
        high_value_count=_to_json_value(_field(record, "previous_high_value_count")),
        no_contact_count=_to_json_value(_field(record, "previous_no_contact_count")),
        deals_this_month_count=_to_json_value(_field(record, "previous_deals_this_month_count")),
        all_deals_count=_to_json_value(_field(record, "previous_all_deals_count")),
        month=_to_json_value(_field(record, "previous_month")),
        ready=_to_json_value(_field(record, "previous_ready")),
    )
    new = PersonListSummaryCounts(
        active_person_count=_require_non_negative_int(
            _field(record, "new_active_person_count"), field="active_person_count"
        ),
        high_risk_count=_require_non_negative_int(
            _field(record, "new_high_risk_count"), field="high_risk_count"
        ),
        high_value_count=_require_non_negative_int(
            _field(record, "new_high_value_count"), field="high_value_count"
        ),
        no_contact_count=_require_non_negative_int(
            _field(record, "new_no_contact_count"), field="no_contact_count"
        ),
        deals_this_month_count=_require_non_negative_int(
            _field(record, "new_deals_this_month_count"), field="deals_this_month_count"
        ),
        all_deals_count=_require_non_negative_int(
            _field(record, "new_all_deals_count"), field="all_deals_count"
        ),
    )
    return PersonListSummaryPublication(
        previous=previous,
        new=new,
        new_month=_require_non_empty_str(_field(record, "new_month"), field="new_month"),
        activated=_require_bool(_field(record, "activated"), field="activated"),
    )


def _field(record: Record, name: str) -> object:
    """Return one raw field of a Neo4j record as a plain object."""
    value: object = record[name]
    return value


def _is_month_marker(value: str) -> bool:
    """Return whether a value looks like a ``YYYY-MM`` month marker."""
    return len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit()


def _field_drift(field: str, previous: JsonValue, current: int) -> PersonListSummaryFieldDrift:
    """Return the absolute drift of one field against its stored previous value."""
    if isinstance(previous, int) and not isinstance(previous, bool):
        return PersonListSummaryFieldDrift(
            field=field,
            previous=previous,
            current=current,
            valid_previous=True,
            drift=abs(current - previous),
        )
    return PersonListSummaryFieldDrift(
        field=field,
        previous=previous,
        current=current,
        valid_previous=False,
        drift=None,
    )


def _require_non_negative_int(value: object, *, field: str) -> int:
    """Return ``value`` as a non-negative integer, rejecting anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"person-list summary {field} is not an integer: {value!r}")
    if value < 0:
        raise RuntimeError(f"person-list summary {field} is negative: {value!r}")
    return value


def _require_bool(value: object, *, field: str) -> bool:
    """Return ``value`` as a boolean, rejecting anything else."""
    if not isinstance(value, bool):
        raise RuntimeError(f"person-list summary {field} is not a boolean: {value!r}")
    return value


def _require_non_empty_str(value: object, *, field: str) -> str:
    """Return ``value`` as a non-empty string, rejecting anything else."""
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"person-list summary {field} is not a non-empty string: {value!r}")
    return value


def _to_json_value(value: object) -> JsonValue:
    """Return a stored counter value as a JSON-compatible value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    raise RuntimeError(f"person-list summary value has unsupported type: {type(value).__name__}")
