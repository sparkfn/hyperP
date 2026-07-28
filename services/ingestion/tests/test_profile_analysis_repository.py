"""Safe graph selection and mapping contracts for the analysis worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from src.graph import queries
from src.profile_analysis_mapping import ProfileAnalysisTemporalMappingError
from src.profile_analysis_repository import (
    ProfileAnalysisMappingError,
    SensitiveGraphRow,
    map_claimed_profile_analysis_people,
    map_known_sensitive_values,
    map_profile_analysis_snapshot_rows,
)
from src.profile_analysis_snapshot import canonical_snapshot_json


@dataclass(slots=True)
class _ClaimState:
    claim_token: str | None = None
    claim_until: datetime | None = None

    def preselect(self, query: str, now: datetime) -> bool:
        assert query is queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES
        return self.claim_until is None or self.claim_until <= now

    def post_lock_claim(
        self,
        query: str,
        *,
        now: datetime,
        claim_token: str,
        claim_until: datetime,
    ) -> bool:
        assert query is queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES
        if self.claim_until is not None and self.claim_until > now:
            return False
        self.claim_token = claim_token
        self.claim_until = claim_until
        return True


@dataclass(slots=True)
class _PublicationState:
    person_status: str
    input_revision: int
    current_sales_analysis_id: str
    attempt_statuses: list[str]

    def persist_sales_success(
        self,
        query: str,
        *,
        captured_revision: int,
        analysis_id: str,
    ) -> str:
        assert query is queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS
        publishable = self.person_status == "active" and self.input_revision == captured_revision
        status = "succeeded" if publishable else "obsolete"
        self.attempt_statuses.append(status)
        if publishable:
            self.current_sales_analysis_id = analysis_id
        return status


def test_claim_query_is_active_bounded_and_uses_an_expiring_lease() -> None:
    query = queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES

    assert "person.status = 'active'" in query
    assert "person.analysis_claim_until <= $now" in query
    assert "LIMIT $batch_size" in query
    assert "person.analysis_claim_token = $claim_token" in query
    assert "person.analysis_claim_until = $claim_until" in query
    claim_lock = "SET person.analysis_claim_until = coalesce("
    post_lock_revalidation = "WHERE person.analysis_claim_until <= $now"
    final_claim = "person.analysis_claim_token = $claim_token"
    assert claim_lock in query
    claim_lock_index = query.index(claim_lock)
    post_lock_revalidation_index = query.index(
        post_lock_revalidation,
        claim_lock_index + len(claim_lock),
    )
    final_claim_index = query.index(
        final_claim,
        post_lock_revalidation_index + len(post_lock_revalidation),
    )
    assert claim_lock_index < post_lock_revalidation_index < final_claim_index
    assert "current_sales IS NULL" in query
    assert "current_contact IS NULL" in query
    assert "current_sales.input_revision < input_revision" in query
    assert "current_contact.input_revision < input_revision" in query


def test_claim_renewal_keeps_the_owner_revision_and_active_status_guarded() -> None:
    query = queries.RENEW_PROFILE_ANALYSIS_CLAIM

    assert "person.analysis_claim_token = $claim_token" in query
    assert "person.status = 'active'" in query
    assert "coalesce(person.analysis_input_revision, 0) = $input_revision" in query
    assert "person.analysis_claim_until = $claim_until" in query


def test_new_profile_analysis_queries_use_scoped_subqueries() -> None:
    queries_under_test = (
        queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES,
        queries.PROFILE_ANALYSIS_WORK_REMAINS,
        queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS,
        queries.MARK_PROFILE_ANALYSIS_DIRTY,
        queries.RETIRE_SOURCE_EVIDENCE,
    )

    assert all("CALL {\n" not in query for query in queries_under_test)


def test_claim_query_keeps_type_retry_state_independent() -> None:
    query = queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES

    assert "sales_failure.retryable = true" in query
    assert "sales_failure.next_retry_at <= $now" in query
    assert "contact_failure.retryable = true" in query
    assert "contact_failure.next_retry_at <= $now" in query
    assert query.count("failed.completed_at DESC, failed.analysis_id DESC") == 2
    assert "sales_due OR contact_due" in query
    assert "CASE WHEN sales_due THEN 'sales'" in query
    assert "CASE WHEN contact_due THEN 'contact_tracing'" in query


def test_claim_query_prioritizes_explicit_invalidations_before_backfill() -> None:
    query = queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES

    priority = "CASE WHEN person.analysis_dirty_at IS NULL THEN 1 ELSE 0 END"
    assert priority in query
    timestamp_order = "coalesce(person.analysis_dirty_at, person.created_at)"
    assert query.index(priority) < query.index(timestamp_order)


def test_expired_claim_is_recovered_by_actual_claim_query_state_model() -> None:
    now = datetime(2026, 7, 21, 2, tzinfo=UTC)
    state = _ClaimState(
        claim_token="dead-worker",
        claim_until=now - timedelta(seconds=1),
    )

    selected = state.preselect(queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES, now)
    claimed = state.post_lock_claim(
        queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES,
        now=now,
        claim_token="recovery-worker",
        claim_until=now + timedelta(minutes=5),
    )

    assert selected is True
    assert claimed is True
    assert state.claim_token == "recovery-worker"


def test_post_lock_revalidation_excludes_duplicate_worker() -> None:
    now = datetime(2026, 7, 21, 2, tzinfo=UTC)
    state = _ClaimState()
    query = queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES

    assert state.preselect(query, now) is True
    assert state.preselect(query, now) is True
    assert state.post_lock_claim(
        query,
        now=now,
        claim_token="worker-a",
        claim_until=now + timedelta(minutes=5),
    )
    assert not state.post_lock_claim(
        query,
        now=now,
        claim_token="worker-b",
        claim_until=now + timedelta(minutes=5),
    )
    assert state.claim_token == "worker-a"


def test_claim_mapping_is_typed_and_preserves_due_type_attempt_numbers() -> None:
    claimed = map_claimed_profile_analysis_people(
        [
            {
                "person_id": "person-1",
                "input_revision": 4,
                "sales_due": True,
                "sales_attempt_number": 2,
                "contact_due": False,
                "contact_attempt_number": 7,
            }
        ]
    )

    assert claimed[0].person_id == "person-1"
    assert claimed[0].input_revision == 4
    assert [(due.analysis_type.value, due.attempt_number) for due in claimed[0].due] == [
        ("sales", 2)
    ]


def test_snapshot_query_returns_only_explicit_safe_structured_columns() -> None:
    query = queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS.lower()
    forbidden = (
        "raw_payload",
        "transcript",
        "normalized_value",
        "full_name",
        "nric",
        "phone",
        "email",
        "postal_code",
        "date_of_birth",
        "properties(",
        "return person",
        "return source",
    )

    assert "$person_id" in query
    assert "source_category" in query
    assert "order_date" in query
    assert "relationship_category" in query
    for fragment in forbidden:
        assert fragment not in query


def test_snapshot_query_uses_real_fact_provenance_and_unknown_age() -> None:
    query = queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS

    assert "null AS age_band" in query
    assert "person.age_band" not in query
    assert "source.quality_flag" not in query
    assert "source.source_trust_tier" not in query
    assert "source.confidence" not in query
    assert "fact.quality_flag" in query
    assert "fact.source_trust_tier" in query
    assert "fact.confidence" in query
    assert "source.extraction_confidence" in query
    assert "coalesce(source.quality_flag, 'valid')" not in query
    assert "coalesce(source.source_trust_tier, 'tier_4')" not in query


def test_snapshot_query_branches_string_native_and_invalid_temporal_values() -> None:
    query = queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS

    for field in (
        "order.ordered_at",
        "source.observed_at",
        "relationship.last_confirmed_at",
    ):
        assert f"WHEN {field} IS NULL THEN null" in query
        assert f"valueType({field}) STARTS WITH 'STRING'" in query
        assert f"THEN toString({field})" in query
        assert f"valueType({field}) STARTS WITH 'DATE'" in query
        assert f"valueType({field}) STARTS WITH 'LOCAL DATETIME'" in query
        assert f"valueType({field}) STARTS WITH 'ZONED DATETIME'" in query
        assert f"THEN toString(date({field}))" in query
    assert query.count("ELSE 'invalid'") == 3


def test_snapshot_query_excludes_retired_sales_and_includes_active_vehicle_mentions() -> None:
    query = queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS

    assert "MATCH (person)-[purchase:PURCHASED]->(order:Order)" in query
    assert "WHERE coalesce(purchase.is_active, true) = true" in query
    assert "MATCH (source:SourceRecord)-[:LINKED_TO]->(person)" in query
    assert "MATCH (source)-[vehicle_link:MENTIONS_VEHICLE]->(vehicle:Vehicle)" in query
    assert "source.lifecycle_status = 'active'" in query
    assert "coalesce(vehicle_link.is_active, true) = true" in query
    assert "'inquired' AS relationship_category" in query
    assert "raw_context" not in query


def test_snapshot_query_excludes_relationships_to_merged_people() -> None:
    query = queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS

    assert query.count("related:Person {status: 'active'}") == 2


def test_snapshot_query_bounds_rows_before_materialization_and_reports_omissions() -> None:
    query = queries.FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS

    for limit in ("LIMIT 20", "LIMIT 8", "LIMIT 5", "LIMIT 10"):
        assert limit in query
    assert "'counts' AS row_kind" in query
    assert "omitted_sources" in query
    assert "omitted_orders" in query
    assert "omitted_order_items" in query
    assert "omitted_vehicles" in query
    assert "omitted_relationships" in query


def test_snapshot_mapping_preserves_query_level_omitted_counts() -> None:
    bundle = map_profile_analysis_snapshot_rows(
        "person-1",
        [
            {
                "row_kind": "counts",
                "omitted_sources": 21,
                "omitted_orders": 13,
                "omitted_order_items": 8,
                "omitted_vehicles": 5,
                "omitted_relationships": 34,
            }
        ],
    )

    assert bundle.snapshot.to_payload()["data_quality"]["omitted_counts"] == {
        "sources": 21,
        "orders": 13,
        "order_items": 8,
        "vehicles": 5,
        "relationships": 34,
    }


def test_snapshot_rows_map_deterministically_into_reviewed_scalar_types() -> None:
    rows = [
        {
            "row_kind": "profile",
            "internal_id": "person-1",
            "parent_internal_id": None,
            "age_band": "35-44",
            "completeness_band": "high",
            "completeness_score": 0.9,
        },
        {
            "row_kind": "order",
            "internal_id": "order-z",
            "parent_internal_id": None,
            "order_date": "2026-07-01",
            "total": 20.0,
            "currency": "SGD",
            "merchant": "Workshop",
            "product": "Tyre",
            "category": "Parts",
        },
        {
            "row_kind": "relationship",
            "internal_id": "knows-z",
            "parent_internal_id": "person-z",
            "relationship_category": "colleague",
            "direction": "outgoing",
            "event_date": "2026-06-03",
        },
    ]

    source = map_profile_analysis_snapshot_rows("person-1", rows)
    serialized = canonical_snapshot_json(source.snapshot)

    assert source.snapshot.orders[0].currency is not None
    assert source.snapshot.orders[0].currency.value == "SGD"
    assert source.snapshot.orders[0].order_date is not None
    assert source.snapshot.orders[0].order_date.value == "2026-07-01"
    assert source.snapshot.orders[0].merchant is not None
    assert source.snapshot.orders[0].merchant.value == "Workshop"
    assert source.snapshot.relationships[0].contact_alias == "Contact A"
    assert "person-1" not in serialized
    assert "person-z" not in serialized


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("order_date", "2022-10-02T18:00:07Z", "2022-10-02"),
        ("observed_date", "2022-10-02T18:00:07+08:00", "2022-10-02"),
        ("event_date", "2022-10-02T18:00:07+0000", "2022-10-02"),
        ("order_date", "2022-10-02T18:00:07", "2022-10-02"),
        (
            "order_date",
            "2022-10-02T18:00:07+08:00[Asia/Manila]",
            "2022-10-02",
        ),
    ),
)
def test_snapshot_mapping_normalizes_iso_datetimes_to_safe_dates(
    field: str,
    value: str,
    expected: str,
) -> None:
    row: dict[str, str | float | None] = {
        "row_kind": "order",
        "internal_id": "order-1",
        "order_date": None,
        "total": None,
        "currency": None,
        "merchant": None,
        "product": None,
        "category": None,
    }
    if field == "observed_date":
        row = {
            "row_kind": "source",
            "internal_id": "source-1",
            "record_type": "sales",
            "source_category": "sales",
            "observed_date": value,
            "quality_flag": "valid",
            "trust_tier": "tier_1",
            "confidence": 0.9,
        }
    elif field == "event_date":
        row = {
            "row_kind": "relationship",
            "internal_id": "relationship-1",
            "parent_internal_id": "person-2",
            "relationship_category": "colleague",
            "direction": "outgoing",
            "event_date": value,
        }
    else:
        row[field] = value

    bundle = map_profile_analysis_snapshot_rows("person-1", [row])

    if field == "order_date":
        assert bundle.snapshot.orders[0].order_date is not None
        assert bundle.snapshot.orders[0].order_date.value == expected
    elif field == "observed_date":
        assert bundle.snapshot.sources[0].observed_date is not None
        assert bundle.snapshot.sources[0].observed_date.value == expected
    else:
        assert bundle.snapshot.relationships[0].event_date is not None
        assert bundle.snapshot.relationships[0].event_date.value == expected


def test_snapshot_mapping_rejects_invalid_temporal_text_with_a_safe_typed_error() -> None:
    row = {
        "row_kind": "order",
        "internal_id": "internal-order",
        "order_date": "not-a-date-private-value",
        "total": None,
        "currency": None,
        "merchant": None,
        "product": None,
        "category": None,
    }

    with pytest.raises(ProfileAnalysisTemporalMappingError) as raised:
        map_profile_analysis_snapshot_rows("person-1", [row])

    assert str(raised.value) == "invalid safe profile analysis snapshot data"
    assert "not-a-date-private-value" not in str(raised.value)


def test_missing_source_provenance_stays_null_and_surfaces_a_data_gap() -> None:
    bundle = map_profile_analysis_snapshot_rows(
        "person-1",
        [
            {
                "row_kind": "source",
                "internal_id": "source-graph-id",
                "record_type": "identity",
                "source_category": "identity",
                "observed_date": "2026-07-01",
                "quality_flag": None,
                "trust_tier": None,
                "confidence": None,
            }
        ],
    )

    payload = bundle.snapshot.to_payload()

    assert payload["sources"][0]["quality_flag"] is None
    assert payload["sources"][0]["trust_tier"] is None
    assert payload["sources"][0]["confidence"] is None
    assert "source_records" in payload["data_quality"]["data_gaps"]


@pytest.mark.parametrize(
    ("field", "value"),
    (("merchant", "<script>"), ("currency", "sgd"), ("order_date", "07/01/2026")),
)
def test_malformed_dynamic_snapshot_values_raise_safe_mapping_errors(
    field: str,
    value: str,
) -> None:
    row = {
        "row_kind": "order",
        "internal_id": "internal-order",
        "parent_internal_id": None,
        "order_date": "2026-07-01",
        "total": 20.0,
        "currency": "SGD",
        "merchant": "Workshop",
        "product": None,
        "category": None,
    }
    row[field] = value

    with pytest.raises(ProfileAnalysisMappingError) as raised:
        map_profile_analysis_snapshot_rows("person-1", [row])

    assert str(raised.value) == "invalid safe profile analysis snapshot data"
    assert value not in str(raised.value)


@pytest.mark.parametrize(
    ("field", "conflicting_value"),
    (
        ("order_date", "2026-07-02"),
        ("total", 21.0),
        ("currency", "USD"),
        ("merchant", "Other Workshop"),
    ),
)
def test_duplicate_order_rows_reject_conflicting_metadata(
    field: str,
    conflicting_value: str | float,
) -> None:
    first = {
        "row_kind": "order",
        "internal_id": "order-1",
        "order_date": "2026-07-01",
        "total": 20.0,
        "currency": "SGD",
        "merchant": "Workshop",
        "product": "Tyre",
        "category": "Parts",
    }
    second = dict(first)
    second[field] = conflicting_value

    with pytest.raises(ProfileAnalysisMappingError) as raised:
        map_profile_analysis_snapshot_rows("person-1", [first, second])

    assert str(raised.value) == "invalid safe profile analysis snapshot data"


def test_duplicate_order_rows_aggregate_unique_items_deterministically() -> None:
    first = {
        "row_kind": "order",
        "internal_id": "order-1",
        "order_date": "2026-07-01",
        "total": 20.0,
        "currency": "SGD",
        "merchant": "Workshop",
        "product": "Tyre",
        "category": "Parts",
    }
    second = {**first, "product": "Bell", "category": "Accessories"}

    bundle = map_profile_analysis_snapshot_rows("person-1", [first, second, first])

    assert [item.product.value for item in bundle.snapshot.orders[0].items if item.product] == [
        "Bell",
        "Tyre",
    ]


def test_sensitive_value_mapping_accepts_an_empty_or_scalar_only_list() -> None:
    assert map_known_sensitive_values({"known_sensitive_values": []}) == ()
    assert map_known_sensitive_values({"known_sensitive_values": ["private", 1234, 2.5]}) == (
        "private",
        1234,
        2.5,
    )


@pytest.mark.parametrize(
    "record",
    (
        None,
        {},
        {"known_sensitive_values": "private"},
        {"known_sensitive_values": [None]},
        {"known_sensitive_values": [True]},
        {"known_sensitive_values": [["private"]]},
        {"known_sensitive_values": [{"nested": "private"}]},
    ),
)
def test_sensitive_value_mapping_fails_closed_without_revealing_values(
    record: SensitiveGraphRow | None,
) -> None:
    with pytest.raises(ProfileAnalysisMappingError) as raised:
        map_known_sensitive_values(record)

    assert str(raised.value) == "invalid profile analysis sensitive value data"
    assert "private" not in str(raised.value)


@pytest.mark.parametrize(
    ("person_status", "current_revision"),
    (("active", 8), ("merged", 7)),
)
def test_publication_race_obsoletes_attempt_and_preserves_current_pointer(
    person_status: str,
    current_revision: int,
) -> None:
    state = _PublicationState(
        person_status=person_status,
        input_revision=current_revision,
        current_sales_analysis_id="analysis-current",
        attempt_statuses=[],
    )

    status = state.persist_sales_success(
        queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS,
        captured_revision=7,
        analysis_id="analysis-stale",
    )

    assert status == "obsolete"
    assert state.attempt_statuses == ["obsolete"]
    assert state.current_sales_analysis_id == "analysis-current"


def test_release_query_requires_the_owning_claim_token() -> None:
    query = queries.RELEASE_PROFILE_ANALYSIS_CLAIM

    assert "person.analysis_claim_token = $claim_token" in query
    assert "person.analysis_claim_token = null" in query
    assert "person.analysis_claim_until = null" in query


def test_claim_parameters_are_temporal_values_not_interpolated_text() -> None:
    query = queries.CLAIM_PROFILE_ANALYSIS_CANDIDATES
    now = datetime(2026, 7, 21, 2, tzinfo=UTC)

    assert "$now" in query and "$claim_until" in query
    assert now.isoformat() not in query


def test_request_claim_query_aggregates_history_before_using_analysis_type() -> None:
    query = queries.CLAIM_PROFILE_ANALYSIS_REQUEST

    aggregation = (
        "WITH person, input_revision, analysis_type, count(DISTINCT history) AS history_count"
    )
    assert aggregation in query
    assert query.index(aggregation) < query.index("RETURN person.person_id AS person_id")
    assert "request.analysis_type = 'sales' AS sales_due" not in query
    assert "request.analysis_type = 'contact_tracing' AS contact_due" not in query
    assert "CASE WHEN analysis_type = 'sales' THEN history_count + 1 ELSE 1 END" in query
    assert (
        "CASE WHEN analysis_type = 'contact_tracing' THEN history_count + 1 ELSE 1 END"
        in query
    )
