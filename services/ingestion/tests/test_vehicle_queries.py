"""Static assertions for the Vehicle graph-query constants.

The original ``test_machine_unit_queries.py`` mixed string-content assertions
on the query constants with mock-transaction tests that drove the *pipeline*
orchestration (``_write_vehicle_observations`` /
``_write_chat_vehicle_observations``). Those orchestration tests were dropped
during the SDD vehicle remodel and are re-added by the tasks that own each
pipeline path: Task 5/6 re-adds the sales-observation path, and Task 7
re-adds the chat-observation path (see the ``_write_chat_vehicle_observations``
suite at the end of this module). What this module covers throughout is the
query-contract surface: that each constant exists, targets ``:Vehicle`` /
``*_VEHICLE`` rels, and encodes the identity, conflict, promotion, and
sales-candidate behaviour described in the SDD task 2 brief.
"""

from __future__ import annotations

from typing import cast

from src.exclusions import ExclusionContext
from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline

# ---------------------------------------------------------------------------
# Exports / surface
# ---------------------------------------------------------------------------


def test_vehicle_query_constants_are_exported() -> None:
    assert queries.UPSERT_VEHICLE
    assert queries.RESOLVE_EXISTING_VEHICLE_FOR_CHAT
    assert queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE
    assert queries.LINK_ORDER_INVOLVES_VEHICLE
    assert queries.LINK_PERSON_BOUGHT_VEHICLE
    assert queries.LINK_SOURCE_RECORD_MENTIONS_VEHICLE
    assert queries.LINK_PERSON_OWNS_VEHICLE
    assert queries.FLAG_VEHICLE_OWNER_CONFLICTS
    assert queries.FIND_VEHICLE_CANDIDATES_FOR_SALES


def test_vehicle_queries_target_vehicle_labels_and_rels() -> None:
    assert ":Vehicle" in queries.UPSERT_VEHICLE
    assert "INVOLVES_VEHICLE" in queries.LINK_ORDER_INVOLVES_VEHICLE
    assert "BOUGHT_VEHICLE" in queries.LINK_PERSON_BOUGHT_VEHICLE
    assert "OWNS_VEHICLE" in queries.LINK_PERSON_OWNS_VEHICLE
    assert "MENTIONS_VEHICLE" in queries.LINK_SOURCE_RECORD_MENTIONS_VEHICLE
    assert "MENTIONS_VEHICLE" in queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE
    assert "OWNS_VEHICLE" in queries.FLAG_VEHICLE_OWNER_CONFLICTS
    assert "INVOLVES_VEHICLE" in queries.FIND_VEHICLE_CANDIDATES_FOR_SALES


def test_vehicle_queries_do_not_reference_machine_unit_surface() -> None:
    """No MachineUnit label or *_UNIT rel types survive in the Vehicle module."""
    for name in (
        "UPSERT_VEHICLE",
        "RESOLVE_EXISTING_VEHICLE_FOR_CHAT",
        "LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE",
        "LINK_ORDER_INVOLVES_VEHICLE",
        "LINK_PERSON_BOUGHT_VEHICLE",
        "LINK_SOURCE_RECORD_MENTIONS_VEHICLE",
        "LINK_PERSON_OWNS_VEHICLE",
        "FLAG_VEHICLE_OWNER_CONFLICTS",
        "FIND_VEHICLE_CANDIDATES_FOR_SALES",
    ):
        query = getattr(queries, name)
        assert "MachineUnit" not in query, name
        assert "_UNIT" not in query.replace("_VEHICLE", ""), name


# ---------------------------------------------------------------------------
# UPSERT_VEHICLE -- identity, conflict, promotion, create
# ---------------------------------------------------------------------------


def test_upsert_vehicle_lta_match_is_global() -> None:
    """LTA matches across sources: no source_system_key gate on lta_match."""
    query = queries.UPSERT_VEHICLE

    assert "OPTIONAL MATCH (lta_match:Vehicle)" in query
    assert "lta_match.normalized_lta_tag = $normalized_lta_tag" in query
    # The LTA branch must NOT scope by source_system_key (cross-source identity).
    lta_branch = query.split("OPTIONAL MATCH (ser_match:Vehicle)")[0]
    assert "ser_match.source_systems" not in lta_branch
    assert "source_system_key IN lta_match" not in lta_branch


def test_upsert_vehicle_serial_match_is_per_source() -> None:
    """Serial fallback is scoped to the caller's source + sku."""
    query = queries.UPSERT_VEHICLE

    assert "$source_system_key IN ser_match.source_systems" in query
    assert "ser_match.normalized_serial_number = $normalized_serial_number" in query
    assert (
        "$product_sku IN coalesce(ser_match.observed_product_skus_s, []) "
        "OR ser_match.product_sku = $product_sku"
        in query
    )


def test_upsert_vehicle_target_is_coalesce_of_lta_and_serial() -> None:
    query = queries.UPSERT_VEHICLE
    assert "coalesce(lta_match, ser_match) AS target" in query


def test_upsert_vehicle_conflict_branch_flags_both_vehicles() -> None:
    """LTA match != serial match flags both Vehicles, no merge of identifiers."""
    query = queries.UPSERT_VEHICLE

    assert "lta_match <> ser_match" in query
    assert "lta_match.conflict_flag = true" in query
    assert "ser_match.conflict_flag = true" in query
    assert "identifier_conflict" in query
    # The fill SET is guarded by `NOT id_conflict` so a conflict never pollutes
    # either Vehicle's identifying fields.
    assert "CASE WHEN NOT id_conflict THEN [1] ELSE [] END" in query


def test_upsert_vehicle_fill_uses_coalesce_and_appends_source_and_sku() -> None:
    query = queries.UPSERT_VEHICLE

    assert "v.normalized_lta_tag = coalesce(v.normalized_lta_tag, $normalized_lta_tag)" in query
    assert (
        "v.normalized_serial_number = "
        "coalesce(v.normalized_serial_number, $normalized_serial_number)"
        in query
    )
    assert "v.product_sku = coalesce(v.product_sku, $product_sku)" in query
    # source_systems appends the caller's source only when not already present.
    assert "$source_system_key IN coalesce(v.source_systems, [])" in query
    assert "coalesce(v.source_systems, []) + [$source_system_key]" in query
    # observed_product_skus_s appends the SKU only when non-null and not present.
    assert "$product_sku IN coalesce(v.observed_product_skus_s, [])" in query
    assert "coalesce(v.observed_product_skus_s, []) + [$product_sku]" in query


def test_upsert_vehicle_creates_new_vehicle_when_no_match() -> None:
    query = queries.UPSERT_VEHICLE

    assert "CREATE (v:Vehicle {vehicle_id: randomUUID(), created_at: $observed_at})" in query
    assert "RETURN v.vehicle_id AS vehicle_id" in query
    assert "coalesce(v.conflict_flag, false) AS conflict" in query


def test_upsert_vehicle_conflict_flag_preserves_existing() -> None:
    """Non-conflict path keeps any pre-existing conflict_flag (coalesce)."""
    query = queries.UPSERT_VEHICLE
    assert "v.conflict_flag = coalesce(v.conflict_flag, id_conflict)" in query


# ---------------------------------------------------------------------------
# RESOLVE_EXISTING_VEHICLE_FOR_CHAT
# ---------------------------------------------------------------------------


def test_resolve_existing_vehicle_for_chat_does_not_create() -> None:
    query = queries.RESOLVE_EXISTING_VEHICLE_FOR_CHAT

    assert "CREATE (" not in query
    assert "MERGE (v:Vehicle" not in query
    assert "MATCH (v:Vehicle)" in query


def test_resolve_existing_vehicle_for_chat_matches_global_lta_or_serial_plus_product_name() -> None:
    query = queries.RESOLVE_EXISTING_VEHICLE_FOR_CHAT

    # Global LTA match (no source-system scoping — chat carries no source ctx).
    assert "v.normalized_lta_tag = $normalized_lta_tag" in query
    # Serial + product-NAME match (not source_systems + product_sku).
    assert "v.normalized_serial_number = $normalized_serial_number" in query
    assert "toLower(trim(v.product)) = toLower(trim($product))" in query
    assert "$product IS NOT NULL" in query
    assert "RETURN collect(DISTINCT v.vehicle_id) AS vehicle_ids" in query
    # Chat source != sales source: must not match by source_systems or product_sku.
    assert "source_systems" not in query
    assert "product_sku" not in query


def test_resolve_existing_vehicle_for_chat_serial_branch_is_no_lta_fallback() -> None:
    """Finding #8: serial+product branch only fires when LTA is also present.

    Cross-source merge key is the LTA tag. Serial+product alone matches
    in-source identity exactly (a duplicate LTA-less unit); chat must not
    bridge across sources on serial+product alone, per spec §3.
    """
    query = queries.RESOLVE_EXISTING_VEHICLE_FOR_CHAT
    # The serial+product branch is the second OR clause — confirm both
    # clauses are guarded by `$normalized_lta_tag IS NOT NULL`.
    lta_required_occurrences = query.count("$normalized_lta_tag IS NOT NULL")
    # Two: once on the global-LTA branch, once on the serial+product branch.
    assert lta_required_occurrences == 1
    assert query.count("$normalized_lta_tag IS NULL") == 1
    assert query.count("v.normalized_lta_tag = $normalized_lta_tag") == 1


# ---------------------------------------------------------------------------
# Link edges
# ---------------------------------------------------------------------------


def test_link_order_involves_vehicle_carries_source_record_pk() -> None:
    query = queries.LINK_ORDER_INVOLVES_VEHICLE

    assert (
        "MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})"
        in query
    )
    assert "MATCH (v:Vehicle {vehicle_id: $vehicle_id})" in query
    assert "MERGE (o)-[rel:INVOLVES_VEHICLE {" in query
    assert "source_record_pk: $source_record_pk" in query
    assert "rel.confidence = $confidence" in query
    assert "rel.quality_flag = $quality_flag" in query


def test_link_person_bought_vehicle_records_currency() -> None:
    query = queries.LINK_PERSON_BOUGHT_VEHICLE

    assert "MATCH (p:Person {person_id: $person_id})" in query
    assert "MATCH (v:Vehicle {vehicle_id: $vehicle_id})" in query
    assert "MERGE (p)-[rel:BOUGHT_VEHICLE {" in query
    assert "source_order_id:   $source_order_id" in query
    assert "rel.first_seen_at = datetime()" in query
    assert "rel.is_active = $is_active" in query
    assert "rel.last_confirmed_at = datetime()" in query


def test_link_person_owns_vehicle_is_idempotent_on_source_record_pk() -> None:
    query = queries.LINK_PERSON_OWNS_VEHICLE

    assert "MERGE (p)-[rel:OWNS_VEHICLE {" in query
    assert "source_system_key: $source_system_key" in query
    assert "source_record_pk:  $source_record_pk" in query
    assert "rel.first_seen_at = datetime()" in query
    assert "rel.last_confirmed_at = datetime()" in query


def test_link_source_record_mentions_vehicle_carries_context() -> None:
    query = queries.LINK_SOURCE_RECORD_MENTIONS_VEHICLE

    assert "MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})" in query
    assert "MERGE (sr)-[rel:MENTIONS_VEHICLE]->(v)" in query
    assert "rel.source_record_id = $source_record_id" in query
    assert "rel.raw_context = $raw_context" in query
    assert "rel.confidence = $confidence" in query
    assert "rel.quality_flag = $quality_flag" in query


def test_link_chat_source_record_mentions_vehicle_links_single_vehicle() -> None:
    query = queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE

    assert "MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})" in query
    assert "MATCH (v:Vehicle {vehicle_id: $vehicle_id})" in query
    assert "MERGE (sr)-[rel:MENTIONS_VEHICLE]->(v)" in query
    assert "rel.last_seen_at = datetime()" in query


# ---------------------------------------------------------------------------
# FLAG_VEHICLE_OWNER_CONFLICTS
# ---------------------------------------------------------------------------


def test_flag_vehicle_owner_conflicts_flags_multiple_active_owners() -> None:
    query = queries.FLAG_VEHICLE_OWNER_CONFLICTS

    assert (
        "MATCH (v:Vehicle)<-[rel:OWNS_VEHICLE {is_active: true}]-(p:Person {status: 'active'})"
        in query
    )
    assert "collect(DISTINCT p.person_id) AS owner_ids" in query
    assert "size(owner_ids) > 1" in query
    assert "v.conflict_reason = 'multiple_active_owners'" in query
    assert "RETURN v.vehicle_id AS vehicle_id, owner_ids AS owner_ids" in query


# ---------------------------------------------------------------------------
# sales.py edits
# ---------------------------------------------------------------------------


def test_merge_order_writes_non_vehicle_lines() -> None:
    query = queries.MERGE_ORDER

    # MERGE_ORDER aligns its SET assignments for readability (multi-space "="),
    # so collapse whitespace before the substring check.
    assert "o.non_vehicle_lines = $non_vehicle_lines" in " ".join(query.split())


def test_clear_superseded_sales_links_deletes_vehicle_rels() -> None:
    query = queries.CLEAR_SUPERSEDED_SALES_LINKS

    assert "INVOLVES_VEHICLE" in query
    assert "BOUGHT_VEHICLE" in query
    # Legacy MachineUnit rel types must be gone.
    assert "INVOLVES_UNIT" not in query
    assert "BOUGHT_UNIT" not in query
    assert ":MachineUnit" not in query


def test_find_vehicle_candidates_for_sales_requires_contact_overlap_and_nric_block() -> None:
    query = queries.FIND_VEHICLE_CANDIDATES_FOR_SALES

    assert (
        "MATCH (sr:SourceRecord {source_record_pk: $sales_source_record_pk, "
        "link_status: 'pending_customer'})"
        in query
    )
    assert "INVOLVES_VEHICLE {source_record_pk: $sales_source_record_pk}" in query
    assert "(v)<-[rel:BOUGHT_VEHICLE|OWNS_VEHICLE]-(p:Person {status: 'active'})" in query
    # Contact-channel overlap is a REQUIRED filter (required MATCH, not OPTIONAL),
    # with separate email/phone branches and per-list kind gates. Vehicle
    # identity alone is NOT enough — a sale with no customer emails and no
    # customer phones yields zero candidates.
    assert "OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(pi:Identifier)" not in query
    # Expand identifiers from the already selective candidate Person. A
    # standalone Identifier MATCH produces a global label scan and held the
    # corrective write transaction beyond the reviewed lock ceiling.
    assert "MATCH (p)-[primary_identifier:IDENTIFIED_BY]->(pi:Identifier)" in query
    assert "MATCH (pi:Identifier)" not in query
    assert "pi.value IN $customer_emails AND pi.kind IN ['email']" in query
    assert "pi.value IN $customer_phones AND pi.kind IN ['mobile','phone']" in query
    # The combined value-list + broadened kind gate must be gone.
    assert (
        "pi.value IN coalesce($customer_emails, []) + coalesce($customer_phones, [])" not in query
    )
    assert "pi.kind IN ['email','phone']" not in query
    assert "collect(DISTINCT pi.kind) AS contact_channels" in query
    assert "ni.kind IN ['nric','nric_hash']" in query
    assert "customer_nric IS NOT NULL\n  AND customer_nric <> ''" in query
    assert "ni.value <> customer_nric" in query
    assert "size(mismatched_nrics) > 0 AS nric_blocked" in query
    assert "RETURN p.person_id AS person_id" in query
    assert "v.vehicle_id AS vehicle_id" in query
    assert "ORDER BY rel_type, rel.is_active DESC, rel.last_confirmed_at DESC" in query


def test_find_vehicle_candidates_for_sales_does_not_reference_machine_units() -> None:
    query = queries.FIND_VEHICLE_CANDIDATES_FOR_SALES

    assert "MachineUnit" not in query
    assert "INVOLVES_UNIT" not in query
    assert "BOUGHT_UNIT" not in query
    assert "OWNS_UNIT" not in query


def test_find_vehicle_candidates_for_sales_excludes_decided_pairs() -> None:
    """A (sale SourceRecord, candidate Person) pair with an existing
    ``MatchDecision`` must be excluded from re-proposal, so the NRIC-block
    NO_MATCH path does not re-record a duplicate decision on every run.

    Mirrors ``persist_match_decision``'s wiring: the decision is linked to the
    sale's ``SourceRecord`` via ``ABOUT_LEFT {entity_type: 'source_record'}``
    and to the candidate ``Person`` via ``ABOUT_RIGHT {entity_type: 'person'}``.
    """
    query = queries.FIND_VEHICLE_CANDIDATES_FOR_SALES

    assert "AND NOT EXISTS {" in query
    assert "(md:MatchDecision)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(sr)" in query
    assert "(md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(p)" in query


# ---------------------------------------------------------------------------
# Chat orchestration: _write_chat_vehicle_observations
#
# Chat never creates a Vehicle. It resolves an existing Vehicle by global LTA
# or serial+product-name, then links the conversation SourceRecord via
# MENTIONS_VEHICLE only when exactly one Vehicle matched.
# ---------------------------------------------------------------------------


class _ResolveRow:
    def __init__(self, vehicle_ids: list[str]) -> None:
        self._ids = vehicle_ids

    def single(self) -> dict[str, list[str]]:
        return {"vehicle_ids": self._ids}


class _NoopRow:
    def single(self) -> None:
        return None


class _ChatTx:
    """Fake ManagedTransaction recording resolve/link runs."""

    def __init__(self, resolve_ids_per_inquiry: list[list[str]]) -> None:
        self._resolve_iter = iter(resolve_ids_per_inquiry)
        self.resolve_calls: list[dict[str, object]] = []
        self.link_calls: list[dict[str, object]] = []

    def run(self, query: str, **kwargs: object) -> _ResolveRow | _NoopRow:
        if query == queries.RESOLVE_EXISTING_VEHICLE_FOR_CHAT:
            self.resolve_calls.append(kwargs)
            return _ResolveRow(next(self._resolve_iter))
        if query == queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE:
            self.link_calls.append(kwargs)
            return _NoopRow()
        raise AssertionError(f"unexpected query: {query}")


def _conversation_envelope(inquiries: list[dict[str, object]]) -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="whatsapp_chat",
        source_record_id="chat-1",
        record_type=RecordType.CONVERSATION,
        ingest_type="batch",
        observed_at="2026-05-14T00:00:00",
        record_hash="hash-1",
        raw_payload={"inquiries": inquiries, "conversation_text": "..."},
        extraction_confidence=0.9,
        extraction_method="llm",
    )


def _make_pipeline() -> IngestPipeline:
    return IngestPipeline(client=cast(Neo4jClient, object()))


def test_write_chat_vehicle_observations_links_when_exactly_one_vehicle_matches() -> None:
    pipeline = _make_pipeline()
    tx = _ChatTx(resolve_ids_per_inquiry=[["v-1"]])
    pipeline._write_chat_vehicle_observations(
        tx,
        envelope=_conversation_envelope(
            [{"vehicle_product": "Honda scooter", "serial_number": "SN-CHAT-1", "notes": "asked"}]
        ),
        source_record_pk="sr-1",
        exclusion_context=ExclusionContext(),
    )
    assert len(tx.resolve_calls) == 1
    assert tx.resolve_calls[0]["product"] == "Honda scooter"
    assert len(tx.link_calls) == 1
    assert tx.link_calls[0]["vehicle_id"] == "v-1"
    assert tx.link_calls[0]["source_record_pk"] == "sr-1"


def test_write_chat_vehicle_observations_no_link_when_two_vehicles_match() -> None:
    pipeline = _make_pipeline()
    tx = _ChatTx(resolve_ids_per_inquiry=[["v-1", "v-2"]])
    pipeline._write_chat_vehicle_observations(
        tx,
        envelope=_conversation_envelope(
            [{"vehicle_product": "Honda scooter", "serial_number": "SN-CHAT-1"}]
        ),
        source_record_pk="sr-1",
        exclusion_context=ExclusionContext(),
    )
    assert len(tx.resolve_calls) == 1
    assert tx.link_calls == []  # ambiguous -> no link


def test_write_chat_vehicle_observations_no_link_when_zero_vehicles_match() -> None:
    pipeline = _make_pipeline()
    tx = _ChatTx(resolve_ids_per_inquiry=[[]])
    pipeline._write_chat_vehicle_observations(
        tx,
        envelope=_conversation_envelope(
            [{"vehicle_product": "Honda scooter", "serial_number": "SN-CHAT-1"}]
        ),
        source_record_pk="sr-1",
        exclusion_context=ExclusionContext(),
    )
    assert len(tx.resolve_calls) == 1
    assert tx.link_calls == []  # no known vehicle -> no link


def test_write_chat_vehicle_observations_skips_non_vehicle_product_mention() -> None:
    """End-to-end: a non-vehicle product mention yields no observation (Task 3
    enforces this at the parser; assert the pipeline does not run resolve/link)."""
    pipeline = _make_pipeline()
    tx = _ChatTx(resolve_ids_per_inquiry=[])
    pipeline._write_chat_vehicle_observations(
        tx,
        envelope=_conversation_envelope(
            [{"vehicle_product": "Open-face helmet", "serial_number": "SN-HELM-1"}]
        ),
        source_record_pk="sr-1",
        exclusion_context=ExclusionContext(),
    )
    assert tx.resolve_calls == []
    assert tx.link_calls == []


def test_write_chat_vehicle_observations_ignores_non_conversation_envelope() -> None:
    """Non-CONVERSATION records skip the chat path entirely."""
    pipeline = _make_pipeline()
    tx = _ChatTx(resolve_ids_per_inquiry=[])
    envelope = SourceRecordEnvelope(
        source_system="whatsapp_chat",
        source_record_id="rec-1",
        record_type=RecordType.IDENTITY,
        ingest_type="batch",
        observed_at="2026-05-14T00:00:00",
        record_hash="hash-1",
        raw_payload={"inquiries": [{"vehicle_product": "Honda scooter", "serial_number": "SN-1"}]},
    )
    pipeline._write_chat_vehicle_observations(
        tx,
        envelope=envelope,
        source_record_pk="sr-1",
        exclusion_context=ExclusionContext(),
    )
    assert tx.resolve_calls == []
    assert tx.link_calls == []
