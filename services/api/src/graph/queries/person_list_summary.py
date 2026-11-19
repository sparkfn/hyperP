"""Cypher for the maintained Person-list summary counter (issue #489).

The list-summary endpoints historically answered from two full-scan
aggregates over every ``Person`` and every ``crm_deal`` ``SourceRecord``.
Those scans are the dominant latency contributor on the person list.

This module holds the schema and read primitives for a single maintained
counter node, ``(:PersonListSummaryCounter {key: 'global'})``, which carries
the six published summary values:

======================== =====================================================
Stored property          Published API field
======================== =====================================================
``active_person_count``  ``all_profiles_count``
``high_risk_count``      ``high_risk_count``
``high_value_count``     ``high_value_count``
``no_contact_count``     ``no_contact_count``
``deals_this_month_count`` ``deals_this_month_count``
``all_deals_count``      ``all_deals_count``
======================== =====================================================

The counter carries only readiness/generation/month metadata besides the six
values. It is *never* trusted on its own. The keyed read returns independent
validity predicates so a malformed value in one group cannot force a fallback
of the other group:

========================= ====================================================
Predicate                Covers
========================= ====================================================
``ready``                Readiness, compared against the boolean literal.
``generation_valid``     The raw ``generation`` is a non-negative integer.
``core_counts_valid``    The four Person-derived counts are non-negative
                         integers.
``crm_counts_valid``     The two CRM deal counts are non-negative integers.
``month_valid``          The stored month marker still matches the database's
                         current month. This is a **CRM-only** concern: the
                         Person-derived counts are month-independent.
========================= ====================================================

``ready`` compares the stored value against the boolean literal rather than
passing the stored value through, so only a stored boolean ``true`` reads as
ready: a missing, null, or non-boolean stored value never does. ``generation``
is returned raw so a malformed generation stays visible instead of being
silently coerced.

Combined validity (everything published is usable) is
``ready AND generation_valid AND core_counts_valid AND crm_counts_valid``.

The **core** fast path depends only on ``ready``, ``generation_valid`` and
``core_counts_valid`` — never on a deal field or on the month marker. The
**CRM** fast path depends on ``ready``, ``generation_valid``,
``crm_counts_valid`` and ``month_valid``.

A missing, unready, malformed, or month-stale counter falls back to the
retained full-scan queries.
"""

from __future__ import annotations

#: Key of the single global counter node. The uniqueness constraint on this
#: key is what makes concurrent bootstrap safe.
PERSON_LIST_SUMMARY_COUNTER_KEY = "global"

#: The four Person-derived counters.
PERSON_LIST_SUMMARY_CORE_FIELDS: tuple[str, ...] = (
    "active_person_count",
    "high_risk_count",
    "high_value_count",
    "no_contact_count",
)

#: The two CRM deal counters.
PERSON_LIST_SUMMARY_CRM_FIELDS: tuple[str, ...] = (
    "deals_this_month_count",
    "all_deals_count",
)

#: The six published counters, in the order the API envelope presents them.
PERSON_LIST_SUMMARY_COUNTER_FIELDS: tuple[str, ...] = (
    PERSON_LIST_SUMMARY_CORE_FIELDS + PERSON_LIST_SUMMARY_CRM_FIELDS
)

#: Cypher fragment rendering the month of a sampled ``current_time`` value.
#:
#: Callers bind ``current_time`` once with ``datetime.realtime()`` so the year
#: and month components cannot straddle a month boundary. The realtime clock
#: uses the database's configured timezone, matching the authoritative CRM
#: aggregate's timezone semantics.
_MONTH_MARKER = (
    "toString(datetime.truncate('month', current_time).year) + '-' + "
    "right('0' + toString(datetime.truncate('month', current_time).month), 2)"
)

#: Cypher fragment yielding the sampled scan month as ``YYYY-MM``.
_SCAN_MONTH_MARKER = "toString(scan_month.year) + '-' + right('0' + toString(scan_month.month), 2)"


def _valid_count_expression(field: str) -> str:
    """Return Cypher asserting one stored value is a non-negative integer."""
    return f"({field} IS NOT NULL AND valueType({field}) STARTS WITH 'INTEGER' AND {field} >= 0)"


_CORE_COUNTS_VALID_EXPRESSION = " AND ".join(
    _valid_count_expression(f"c.{field}") for field in PERSON_LIST_SUMMARY_CORE_FIELDS
)
_CRM_COUNTS_VALID_EXPRESSION = " AND ".join(
    _valid_count_expression(f"c.{field}") for field in PERSON_LIST_SUMMARY_CRM_FIELDS
)


_CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT = """
CREATE CONSTRAINT person_list_summary_counter_key_unique IF NOT EXISTS
FOR (c:PersonListSummaryCounter) REQUIRE c.key IS UNIQUE
"""

# Idempotent bootstrap of the counter singleton.
#
# ``ON CREATE SET`` only: a concurrent bootstrap or an ordinary restart must
# never reset an already-initialized counter, and must never promote an
# unready counter to ready. Publication of the six values happens exclusively
# through the explicit backfill/reconciliation path.
_ENSURE_PERSON_LIST_SUMMARY_COUNTER = """
MERGE (c:PersonListSummaryCounter {key: $key})
ON CREATE SET c.ready = false,
              c.generation = 0,
              c.active_person_count = 0,
              c.high_risk_count = 0,
              c.high_value_count = 0,
              c.no_contact_count = 0,
              c.deals_this_month_count = 0,
              c.all_deals_count = 0,
              c.updated_at = datetime()
RETURN c.ready AS ready,
       c.generation AS generation
"""

# Keyed read of the counter singleton.
#
# Readiness is compared against the boolean literal so a non-boolean stored
# value can never read as ready; the ``valueType`` guard additionally makes the
# result strictly boolean, because in Cypher a null comparison yields null
# rather than false. ``generation`` is returned raw so a malformed generation
# is visible rather than silently coerced, and ``generation_valid`` states
# separately whether it is a usable non-negative integer.
#
# ``month_valid`` compares the stored month marker against the database's
# current month. It is a CRM-only concern: a stale marker means the published
# month-scoped deal count no longer describes the current month, but it says
# nothing about the Person-derived counts.
#
# The match is optional, so the row is returned even when the singleton does
# not exist: every validity flag is then false and only ``current_month`` — the
# database's own current month — carries a value.
_GET_PERSON_LIST_SUMMARY_COUNTER = f"""
OPTIONAL MATCH (c:PersonListSummaryCounter {{key: $key}})
WITH c, datetime.realtime() AS current_time
WITH c, {_MONTH_MARKER} AS current_month
RETURN (valueType(c.ready) STARTS WITH 'BOOLEAN' AND c.ready = true) AS ready,
       c.generation AS generation,
       ({_valid_count_expression("c.generation")}) AS generation_valid,
       c.month AS month,
       (c.month IS NOT NULL AND c.month = current_month) AS month_valid,
       current_month AS current_month,
       c.active_person_count AS active_person_count,
       c.high_risk_count AS high_risk_count,
       c.high_value_count AS high_value_count,
       c.no_contact_count AS no_contact_count,
       c.deals_this_month_count AS deals_this_month_count,
       c.all_deals_count AS all_deals_count,
       ({_CORE_COUNTS_VALID_EXPRESSION}) AS core_counts_valid,
       ({_CRM_COUNTS_VALID_EXPRESSION}) AS crm_counts_valid
"""

_GET_PERSON_LIST_SUMMARY_CURRENT_MONTH = f"""
WITH datetime.realtime() AS current_time
RETURN {_MONTH_MARKER} AS current_month
"""

CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT = _CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT
ENSURE_PERSON_LIST_SUMMARY_COUNTER = _ENSURE_PERSON_LIST_SUMMARY_COUNTER
GET_PERSON_LIST_SUMMARY_COUNTER = _GET_PERSON_LIST_SUMMARY_COUNTER
GET_PERSON_LIST_SUMMARY_CURRENT_MONTH = _GET_PERSON_LIST_SUMMARY_CURRENT_MONTH

# Capture of the Person-derived contributions of the affected Person nodes.
#
# The statement is bounded by the requested ids: every requested id yields
# exactly one row, and a node that does not exist yields the same
# non-contributing flags as a node that fails every predicate. All four flags
# carry the qualifying-status predicate, because the authoritative aggregate
# sums them over the merged-excluded Person set only.
#
# The ``SET`` is a semantic no-op write: it re-writes the status it reads, so
# the Person node's write lock is held from this before-snapshot until the
# after-snapshot and the delta commit of the same transaction. Two writers
# touching the same Person serialize instead of computing a delta from stale
# state.
_CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS = """
UNWIND $person_ids AS person_id
OPTIONAL MATCH (p:Person {person_id: person_id})
SET p.status = p.status
RETURN person_id AS person_id,
       (p IS NOT NULL AND p.status IS NOT NULL AND p.status <> 'merged')
         AS qualifying_status,
       (p IS NOT NULL AND p.status IS NOT NULL AND p.status <> 'merged'
         AND coalesce(p.is_high_risk = true, false)) AS high_risk,
       (p IS NOT NULL AND p.status IS NOT NULL AND p.status <> 'merged'
         AND coalesce(p.is_high_value = true, false)) AS high_value,
       (p IS NOT NULL AND p.status IS NOT NULL AND p.status <> 'merged'
         AND p.preferred_phone IS NULL AND p.preferred_email IS NULL)
         AS no_contact
"""

# Capture of the distinct-deal contribution of the affected SourceRecords.
#
# A deal contributes when it is a ``crm_deal`` whose lifecycle is active (or
# legacy-active), and when at least one active-or-legacy-active ``LINKED_TO``
# edge reaches a Person whose status is not null and not ``merged``. The
# deal is evaluated once: the ``EXISTS`` subquery replaces a per-edge join.
# No history-family or source filter applies here: the global summary counts
# every distinct qualifying deal.
#
# ``observed_at`` is rendered as a string only for a qualifying deal, so the
# month-scoped contribution is carried by the qualifying deals alone.
#
# ``resolved_element_id`` carries the element id of the matched node (null when
# the selector matched nothing), so the caller can key the snapshot by the
# node itself instead of by the selector it happened to use.
#
# The ``SET`` is a semantic no-op write: it re-writes the lifecycle status it
# reads, so the shared-deal node's write lock is held from this
# before-snapshot until the after-snapshot and the delta commit of the same
# transaction. Two writers touching the same deal serialize instead of
# computing a delta from stale state.
_CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS = """
UNWIND $source_record_pks AS source_record_pk
OPTIONAL MATCH (deal:SourceRecord {source_record_pk: source_record_pk})
SET deal.lifecycle_status = deal.lifecycle_status
WITH source_record_pk, deal
WITH source_record_pk,
     deal,
     coalesce((deal IS NOT NULL
      AND deal.record_type = 'crm_deal'
      AND (deal.lifecycle_status = 'active'
           OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
      AND EXISTS {
        MATCH (deal)-[deal_link:LINKED_TO]->(linked_person:Person)
        WHERE coalesce(deal_link.is_active, true) = true
          AND linked_person.status IS NOT NULL
          AND linked_person.status <> 'merged'
      }), false) AS qualifying
RETURN source_record_pk AS source_record_pk,
       elementId(deal) AS resolved_element_id,
       qualifying AS qualifying,
       CASE WHEN qualifying THEN toString(deal.observed_at) ELSE null END AS observed_at
"""

# Capture of the distinct-deal contribution of the affected SourceRecords,
# addressed by element id.
#
# A distinct deal may carry no ``source_record_pk`` (null or empty), so the pk
# selector cannot address it at all. This variant addresses the node by its
# element id instead: ``UNWIND`` the selectors, then match the node whose
# element id equals the selector.
#
# The predicates are the same as the pk variant, including the semantic no-op
# ``SET`` that holds the shared-deal node's write lock across the snapshot.
_CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID = """
UNWIND $deal_element_ids AS deal_element_id
OPTIONAL MATCH (deal:SourceRecord) WHERE elementId(deal) = deal_element_id
SET deal.lifecycle_status = deal.lifecycle_status
WITH deal_element_id, deal
WITH deal_element_id,
     deal,
     coalesce((deal IS NOT NULL
      AND deal.record_type = 'crm_deal'
      AND (deal.lifecycle_status = 'active'
           OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
      AND EXISTS {
        MATCH (deal)-[deal_link:LINKED_TO]->(linked_person:Person)
        WHERE coalesce(deal_link.is_active, true) = true
          AND linked_person.status IS NOT NULL
          AND linked_person.status <> 'merged'
      }), false) AS qualifying
RETURN deal_element_id AS deal_element_id,
       elementId(deal) AS resolved_element_id,
       qualifying AS qualifying,
       CASE WHEN qualifying THEN toString(deal.observed_at) ELSE null END AS observed_at
"""

# Application of one transaction's net delta to the counter.
#
# The statement runs as one write transaction:
#
# 1. ``MATCH`` the existing global counter.
# 2. ``SET c.updated_at`` takes the counter node's write lock first, so
#    concurrent writers cannot interleave their deltas.
# 3. The database month boundary is sampled *after* that lock, and the same
#    boundary decides which observed_at strings count towards the month.
# 4. The month-scoped contribution is the difference between the qualifying
#    observed_at strings of the after and the before snapshot. A string that
#    does not parse, or that is null, contributes nothing; a future date
#    contributes, exactly like the authoritative aggregate.
# 5. The core values and the all-deals value are updated only when the
#    counter is ready and the relevant stored values are usable. The
#    month-scoped value is updated only when the counter is ready, the stored
#    month still equals the post-lock month, and the stored value is usable.
# 6. The current month is rechecked right before the final ``SET``, so a
#    midnight crossing cannot publish a wrong monthly delta.
#
# The readiness flag, the generation and the month marker are never written:
# this statement only moves the counts. Every value is preserved when the
# counter is unready, stale or malformed; only ``updated_at`` moves.
_APPLY_PERSON_LIST_SUMMARY_DELTAS = f"""
MATCH (c:PersonListSummaryCounter {{key: $key}})
SET c.updated_at = datetime()
WITH c,
     datetime.truncate('month', datetime.realtime()) AS scan_month
WITH c,
     scan_month,
     {_SCAN_MONTH_MARKER} AS current_month_key,
     size([
       value IN $before_observed_at
       WHERE datetime(value) >= scan_month
     ]) AS before_month_count,
     size([
       value IN $after_observed_at
       WHERE datetime(value) >= scan_month
     ]) AS after_month_count
WITH c,
     scan_month,
     current_month_key,
     (after_month_count - before_month_count) AS monthly_delta,
     (valueType(c.ready) STARTS WITH 'BOOLEAN' AND c.ready = true) AS ready_valid,
     ({_valid_count_expression("c.active_person_count")}) AS active_valid,
     ({_valid_count_expression("c.high_risk_count")}) AS high_risk_valid,
     ({_valid_count_expression("c.high_value_count")}) AS high_value_valid,
     ({_valid_count_expression("c.no_contact_count")}) AS no_contact_valid,
     ({_valid_count_expression("c.all_deals_count")}) AS all_deals_valid,
     ({_valid_count_expression("c.deals_this_month_count")}) AS monthly_valid,
     (c.month IS NOT NULL AND c.month = current_month_key) AS month_current,
     datetime.truncate('month', datetime.realtime()) AS final_month
WITH c,
     current_month_key,
     monthly_delta,
     (ready_valid AND active_valid AND high_risk_valid AND high_value_valid
       AND no_contact_valid) AS core_applicable,
     (ready_valid AND all_deals_valid) AS all_deals_applicable,
     (ready_valid AND monthly_valid AND month_current
       AND final_month = scan_month) AS monthly_applicable
SET c.active_person_count = CASE
      WHEN core_applicable THEN c.active_person_count + $active_person_delta
      ELSE c.active_person_count
    END,
    c.high_risk_count = CASE
      WHEN core_applicable THEN c.high_risk_count + $high_risk_delta
      ELSE c.high_risk_count
    END,
    c.high_value_count = CASE
      WHEN core_applicable THEN c.high_value_count + $high_value_delta
      ELSE c.high_value_count
    END,
    c.no_contact_count = CASE
      WHEN core_applicable THEN c.no_contact_count + $no_contact_delta
      ELSE c.no_contact_count
    END,
    c.all_deals_count = CASE
      WHEN all_deals_applicable THEN c.all_deals_count + $all_deals_delta
      ELSE c.all_deals_count
    END,
    c.deals_this_month_count = CASE
      WHEN monthly_applicable THEN c.deals_this_month_count + monthly_delta
      ELSE c.deals_this_month_count
    END
RETURN (valueType(c.ready) STARTS WITH 'BOOLEAN' AND c.ready = true) AS ready,
       c.active_person_count AS active_person_count,
       c.high_risk_count AS high_risk_count,
       c.high_value_count AS high_value_count,
       c.no_contact_count AS no_contact_count,
       c.all_deals_count AS all_deals_count,
       c.deals_this_month_count AS deals_this_month_count,
       current_month_key AS current_month_key,
       core_applicable AS core_applied,
       all_deals_applicable AS all_deals_applied,
       monthly_applicable AS monthly_applied
"""

# Selection of the distinct deals linked to the given Persons.
#
# The statement is bounded by the requested person ids and returns one row per
# distinct deal, addressed by element id. Only CRM deals are selected and
# only through an active-or-legacy-active link, because those are the only
# records whose distinct-deal contribution can change.
_FIND_DEAL_ELEMENT_IDS_FOR_PERSONS = """
UNWIND $person_ids AS person_id
MATCH (deal:SourceRecord {record_type: 'crm_deal'})
      -[link:LINKED_TO]->(:Person {person_id: person_id})
WHERE coalesce(link.is_active, true) = true
RETURN DISTINCT elementId(deal) AS deal_element_id
"""

CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS = _CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS
CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS = _CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS
CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID = _CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID
APPLY_PERSON_LIST_SUMMARY_DELTAS = _APPLY_PERSON_LIST_SUMMARY_DELTAS
FIND_DEAL_ELEMENT_IDS_FOR_PERSONS = _FIND_DEAL_ELEMENT_IDS_FOR_PERSONS
