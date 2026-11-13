"""Cypher for the maintained Person-list summary counter (issue #489).

The person-list summary is served from a single maintained counter node,
``(:PersonListSummaryCounter {key: 'global'})``, which carries the six
published summary values plus readiness/generation/month metadata.

The six values map to the published API envelope as:

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

This module mirrors ``services/api/src/graph/queries/person_list_summary.py``
so the two services stay independent (no cross-service imports), following the
existing duplicated CRM-count query-module precedent.

The recompute statement recomputes the six authoritative aggregates and
publishes them together with the month marker, the readiness flag, and the
update timestamp. It runs as a single write transaction: the counter node is
locked first, the month boundary is sampled once after that lock, and the
publication is abandoned when the database month moves before the commit.
"""

from __future__ import annotations

#: Key of the single global counter node. The uniqueness constraint on this
#: key is what makes concurrent bootstrap safe.
PERSON_LIST_SUMMARY_COUNTER_KEY = "global"

#: The six published counters, in the order the API envelope presents them.
PERSON_LIST_SUMMARY_COUNTER_FIELDS: tuple[str, ...] = (
    "active_person_count",
    "high_risk_count",
    "high_value_count",
    "no_contact_count",
    "deals_this_month_count",
    "all_deals_count",
)

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


def _month_marker_expression(month_value: str) -> str:
    """Return Cypher rendering a month datetime as the ``YYYY-MM`` marker.

    The marker format matches the API's keyed read so the published marker and
    the reader's current-month marker are directly comparable.
    """
    return f"toString({month_value}.year) + '-' + right('0' + toString({month_value}.month), 2)"


#: Cypher fragment rendering the month of a sampled ``current_time`` value.
#:
#: Callers bind ``current_time`` once with ``datetime.realtime()`` so the year
#: and month components cannot straddle a month boundary. The realtime clock
#: uses the database's configured timezone, matching the authoritative CRM
#: aggregate's timezone semantics.
_MONTH_MARKER = _month_marker_expression("current_time")

#: Cypher fragment yielding the sampled scan month as ``YYYY-MM``.
_SCAN_MONTH_MARKER = _month_marker_expression("scan_month")


def _valid_count_expression(field: str) -> str:
    """Return Cypher asserting one stored value is a non-negative integer."""
    return f"({field} IS NOT NULL AND valueType({field}) STARTS WITH 'INTEGER' AND {field} >= 0)"


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

# Authoritative recompute and publication of the counter.
#
# The statement runs as one write transaction:
#
# 1. ``MATCH`` the existing global counter.
# 2. ``SET c.updated_at`` takes the counter node's write lock, so concurrent
#    publications cannot interleave with this one.
# 3. The previous values are captured *after* that lock, so they describe the
#    committed state this publication is about to replace.
# 4. The database month boundary is sampled once, after the lock, and the same
#    boundary drives the month-scoped deal aggregate.
# 5. The four Person aggregates and the two distinct-deal aggregates are
#    computed in separate subqueries: no Cartesian product between them and
#    no unbounded collection of rows.
# 6. If the database month moved again before the commit, no row is returned
#    and nothing is published, so the caller can retry against a fresh month.
# 7. Otherwise all six counts, the month marker, the readiness flag, and the
#    update timestamp are published together.
#
# ``$activate`` is set only by the explicit operator backfill. The nightly
# reconciliation leaves it false, so an initially unready counter stays unready.
_RECOMPUTE_PERSON_LIST_SUMMARY = f"""
MATCH (c:PersonListSummaryCounter {{key: $key}})
SET c.updated_at = datetime()
WITH c,
     c.active_person_count AS previous_active_person_count,
     c.high_risk_count AS previous_high_risk_count,
     c.high_value_count AS previous_high_value_count,
     c.no_contact_count AS previous_no_contact_count,
     c.deals_this_month_count AS previous_deals_this_month_count,
     c.all_deals_count AS previous_all_deals_count,
     c.month AS previous_month,
     c.ready AS previous_ready,
     datetime.truncate('month', datetime.realtime()) AS scan_month
WITH c,
     previous_active_person_count,
     previous_high_risk_count,
     previous_high_value_count,
     previous_no_contact_count,
     previous_deals_this_month_count,
     previous_all_deals_count,
     previous_month,
     previous_ready,
     scan_month,
     {_SCAN_MONTH_MARKER} AS scan_month_key
WHERE ($activate = true)
   OR (valueType(c.ready) STARTS WITH 'BOOLEAN' AND c.ready = true)
CALL () {{
  MATCH (p:Person)
  WHERE p.status <> 'merged'
  RETURN count(p) AS active_person_count,
         coalesce(
           sum(CASE WHEN p.is_high_risk = true THEN 1 ELSE 0 END),
           0
         ) AS high_risk_count,
         coalesce(
           sum(CASE WHEN p.is_high_value = true THEN 1 ELSE 0 END),
           0
         ) AS high_value_count,
         coalesce(
           sum(CASE WHEN p.preferred_phone IS NULL AND p.preferred_email IS NULL
                    THEN 1 ELSE 0 END),
           0
         ) AS no_contact_count
}}
CALL (scan_month) {{
  MATCH (deal:SourceRecord {{record_type: 'crm_deal'}})
  WHERE (deal.lifecycle_status = 'active'
      OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
    AND EXISTS {{
      MATCH (deal)-[deal_link:LINKED_TO]->(p:Person)
      WHERE p.status <> 'merged'
        AND coalesce(deal_link.is_active, true) = true
    }}
  RETURN count(DISTINCT deal) AS all_deals_count,
         count(DISTINCT CASE WHEN deal.observed_at >= scan_month THEN deal END)
           AS deals_this_month_count
}}
WITH c,
     previous_active_person_count,
     previous_high_risk_count,
     previous_high_value_count,
     previous_no_contact_count,
     previous_deals_this_month_count,
     previous_all_deals_count,
     previous_month,
     previous_ready,
     scan_month_key,
     active_person_count,
     high_risk_count,
     high_value_count,
     no_contact_count,
     all_deals_count,
     deals_this_month_count,
     datetime.realtime() AS current_time
WITH c,
     previous_active_person_count,
     previous_high_risk_count,
     previous_high_value_count,
     previous_no_contact_count,
     previous_deals_this_month_count,
     previous_all_deals_count,
     previous_month,
     previous_ready,
     scan_month_key,
     active_person_count,
     high_risk_count,
     high_value_count,
     no_contact_count,
     all_deals_count,
     deals_this_month_count,
     {_MONTH_MARKER} AS current_month_key
WHERE current_month_key = scan_month_key
SET c.active_person_count = active_person_count,
    c.high_risk_count = high_risk_count,
    c.high_value_count = high_value_count,
    c.no_contact_count = no_contact_count,
    c.deals_this_month_count = deals_this_month_count,
    c.all_deals_count = all_deals_count,
    c.month = scan_month_key,
    c.ready = (($activate = true)
      OR (valueType(c.ready) STARTS WITH 'BOOLEAN' AND c.ready = true)),
    c.updated_at = datetime()
RETURN previous_active_person_count AS previous_active_person_count,
       previous_high_risk_count AS previous_high_risk_count,
       previous_high_value_count AS previous_high_value_count,
       previous_no_contact_count AS previous_no_contact_count,
       previous_deals_this_month_count AS previous_deals_this_month_count,
       previous_all_deals_count AS previous_all_deals_count,
       previous_month AS previous_month,
       previous_ready AS previous_ready,
       active_person_count AS new_active_person_count,
       high_risk_count AS new_high_risk_count,
       high_value_count AS new_high_value_count,
       no_contact_count AS new_no_contact_count,
       deals_this_month_count AS new_deals_this_month_count,
       all_deals_count AS new_all_deals_count,
       scan_month_key AS new_month,
       ($activate = true
         AND NOT (valueType(previous_ready) STARTS WITH 'BOOLEAN'
                  AND previous_ready = true)) AS activated
"""

# Invalidation of the counter.
#
# The counts and the month marker are preserved so a later backfill can report
# the drift it repaired; only readiness and the generation move. The generation
# is advanced through a guarded ``CASE``: a malformed generation is replaced
# with a valid one instead of failing the invalidation.
_INVALIDATE_PERSON_LIST_SUMMARY = """
MATCH (c:PersonListSummaryCounter {key: $key})
SET c.ready = false,
    c.generation = CASE
      WHEN valueType(c.generation) STARTS WITH 'INTEGER' AND c.generation >= 0
        THEN c.generation + 1
      ELSE 1
    END,
    c.updated_at = datetime()
RETURN c.ready AS ready,
       c.generation AS generation
"""

# Keyed read of the counter's publication state.
#
# Used to tell an unready skip apart from a month-boundary abort after a
# recompute attempt published no row. The stored values are returned raw: the
# caller decides whether they are usable.
_GET_PERSON_LIST_SUMMARY_PUBLICATION_STATE = """
MATCH (c:PersonListSummaryCounter {key: $key})
RETURN c.ready AS ready,
       c.month AS month,
       c.generation AS generation
"""

# Capture of the Person-derived contributions of the affected Person nodes.
#
# The statement is bounded by the requested ids: every requested id yields
# exactly one row, and a node that does not exist yields the same
# non-contributing flags as a node that fails every predicate. The four
# flags mirror the authoritative Person aggregate:
#
# * ``qualifying_status``: the status is not null and is not ``merged``.
# * ``high_risk`` / ``high_value``: the flag is exactly true.
# * ``no_contact``: both the stored phone and the stored email are null.
#
# All four flags carry the qualifying-status predicate: the authoritative
# aggregate sums its flags over the merged-excluded Person set only, so a
# merged Person contributes to none of them.
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
# The predicates are the same as the pk variant: the same record type, the
# same lifecycle rule, the same single ``EXISTS`` over an active-or-legacy-
# active ``LINKED_TO`` edge to a non-merged Person, and no history-family or
# source filter. The semantic no-op ``SET`` that holds the shared-deal node's
# write lock across the snapshot is applied here too.
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

# Application of one writer transaction's net delta to the counter.
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
     datetime.realtime() AS current_time
WITH c,
     scan_month,
     current_month_key,
     monthly_delta,
     ready_valid,
     active_valid,
     high_risk_valid,
     high_value_valid,
     no_contact_valid,
     all_deals_valid,
     monthly_valid,
     month_current,
     current_time,
     {_MONTH_MARKER} AS final_month_key
WITH c,
     current_month_key,
     scan_month,
     monthly_delta,
     current_time,
     (ready_valid AND active_valid AND high_risk_valid AND high_value_valid
       AND no_contact_valid) AS core_applicable,
     (ready_valid AND all_deals_valid) AS all_deals_applicable,
     (ready_valid AND monthly_valid AND month_current
       AND final_month_key = current_month_key
       AND datetime.truncate('month', current_time) = scan_month) AS monthly_applicable
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

CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT = _CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT
ENSURE_PERSON_LIST_SUMMARY_COUNTER = _ENSURE_PERSON_LIST_SUMMARY_COUNTER
RECOMPUTE_PERSON_LIST_SUMMARY = _RECOMPUTE_PERSON_LIST_SUMMARY
INVALIDATE_PERSON_LIST_SUMMARY = _INVALIDATE_PERSON_LIST_SUMMARY
GET_PERSON_LIST_SUMMARY_PUBLICATION_STATE = _GET_PERSON_LIST_SUMMARY_PUBLICATION_STATE
CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS = _CAPTURE_PERSON_SUMMARY_CONTRIBUTIONS
CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS = _CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS
CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID = _CAPTURE_DEAL_SUMMARY_CONTRIBUTIONS_BY_ELEMENT_ID
APPLY_PERSON_LIST_SUMMARY_DELTAS = _APPLY_PERSON_LIST_SUMMARY_DELTAS
