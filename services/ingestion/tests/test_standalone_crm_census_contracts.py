"""Deterministic contract coverage for census transitions without a live graph."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from src.celery_app import _beat_schedule, celery_app
from src.graph.queries import standalone_crm_census as queries
from src.graph.queries.bitrix_source_instances import CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS
from src.graph.queries.standalone_crm_census_schema import CENSUS_CONSTRAINT_SPECS
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStatus,
    StandaloneCrmPublication,
)
from src.standalone_crm_census_models import (
    StandaloneCrmBudgetSnapshot,
    StandaloneCrmTerminalAccounting,
)
from src.standalone_crm_census_requests import SourceSyncAuthoritySnapshot, SourceSyncCensusRequest
from src.standalone_crm_census_runtime import StandaloneCrmCensusRuntime
from standalone_crm_census_neo4j_support import CONTROL_MIGRATION_BASE_CONSTRAINTS


class _Admission:
    def admit(self, *, control_instance_id: str, source_instance_id: str) -> None:
        del control_instance_id, source_instance_id


class _Authority:
    def source_sync_heads(self, request: object) -> SourceSyncAuthoritySnapshot:
        del request
        return SourceSyncAuthoritySnapshot("mapping", "digest", None)

    def validate_mapping_prepare(self, request: object) -> None:
        del request

    def validate_mapping_rollback(self, request: object) -> None:
        del request


class _Publisher:
    def __init__(self, available: bool) -> None:
        self.available = available
        self.calls: list[tuple[str, str, str, str]] = []

    def handler_available(self, census_kind: str, unit_kind: str) -> bool:
        assert census_kind == "source_sync"
        assert unit_kind == "contact"
        return self.available

    def publish(self, *, task_name: str, task_id: str, queue: str, payload_json: str) -> None:
        self.calls.append((task_name, task_id, queue, payload_json))


class _Repository:
    def __init__(self, *, observed: str = "none") -> None:
        self.admission = StandaloneCrmCensusAdmission(
            "census", "publishing", "fingerprint", "authority", "source", "control", False
        )
        self.publication = StandaloneCrmPublication(
            "publication",
            "task",
            '{"generation":1,"unit_kind":"contact"}',
            "sha256:payload",
            "child",
            "ingestion",
            "ambiguous",
        )
        self.observed = observed
        self.confirmed = 0
        self.publishing = 0
        self.ambiguous = 0
        self.terminalized = 0
        self.stale_marked = 0

    def publication_recovery(
        self, publication_id: str
    ) -> tuple[StandaloneCrmCensusAdmission, StandaloneCrmPublication, str]:
        assert publication_id == "publication"
        return self.admission, self.publication, self.observed

    def load_admitted_request(
        self, census_id: str
    ) -> tuple[StandaloneCrmCensusAdmission, SourceSyncCensusRequest, SourceSyncAuthoritySnapshot]:
        assert census_id == "census"
        return (
            self.admission,
            SourceSyncCensusRequest(
                "bitrix_chat",
                "source",
                "control",
                "occurrence",
                "operator",
                ("contact",),
                "policy",
                "association",
                "digest",
                StandaloneCrmBudgetSnapshot(1, 1, 1.0, 1, 1, 1, 2.0),
            ),
            SourceSyncAuthoritySnapshot("mapping", "digest", None),
        )

    def status(self, census_id: str) -> StandaloneCrmCensusStatus:
        assert census_id == "census"
        deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        return StandaloneCrmCensusStatus(
            {"census_kind": "source_sync"},
            (
                {
                    "census_id": "census",
                    "generation": 1,
                    "task_id": "parent",
                    "state": "running",
                    "fence_token": 1,
                    "deadline_at": deadline,
                    "occurrence_deadline_at": deadline,
                },
            ),
            (),
            (),
            (),
        )

    def mark_publication_publishing(
        self, admission: object, attempt: object, publication_id: str
    ) -> None:
        del admission, attempt
        assert publication_id == "publication"
        self.publishing += 1

    def authorize_publication_broker(self, *args: object) -> None:
        del args

    def mark_publication_ambiguous(
        self, admission: object, attempt: object, publication_id: str
    ) -> None:
        del admission, attempt, publication_id
        self.ambiguous += 1

    def confirm_publication(self, admission: object, attempt: object, publication_id: str) -> None:
        del admission, attempt
        assert publication_id == "publication"
        self.confirmed += 1

    def confirm_observed_publication(
        self, admission: object, attempt: object, publication_id: str
    ) -> None:
        del admission, attempt, publication_id
        self.confirmed += 1

    def mark_authority_stale(self, admission: object) -> None:
        del admission
        self.stale_marked += 1

    def reconcile_terminal(
        self, admission: object, attempt: object
    ) -> tuple[str, StandaloneCrmTerminalAccounting]:
        del admission, attempt
        self.terminalized += 1
        return "completed", StandaloneCrmTerminalAccounting(1, 0, 0, 0, 1)


def _runtime(repository: _Repository, publisher: _Publisher) -> StandaloneCrmCensusRuntime:
    return StandaloneCrmCensusRuntime(
        repository=repository,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=_Authority(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: (_ for _ in ()).throw(AssertionError("no probe")),
    )


def test_publication_repair_republishes_exact_immutable_outbox_payload() -> None:
    repository = _Repository()
    publisher = _Publisher(True)
    _runtime(repository, publisher).repair_publication("publication")
    assert publisher.calls == [
        ("child", "task", "ingestion", '{"generation":1,"unit_kind":"contact"}')
    ]
    assert repository.publishing == 1
    assert repository.confirmed == 1


def test_observed_child_repairs_without_a_second_publish() -> None:
    repository = _Repository(observed="fence_claim")
    publisher = _Publisher(False)
    _runtime(repository, publisher).repair_publication("publication")
    assert repository.confirmed == 1
    assert repository.publishing == 0
    assert publisher.calls == []


def test_publication_repair_never_publishes_when_handler_is_absent() -> None:
    repository = _Repository()
    publisher = _Publisher(False)
    with pytest.raises(RuntimeError, match="handler is unavailable"):
        _runtime(repository, publisher).repair_publication("publication")
    assert publisher.calls == []
    assert repository.publishing == 0


def test_query_contracts_include_fences_recovery_and_authority_guards() -> None:
    assert "StandaloneCrmCensusScopeLock" in queries.ADMIT_CENSUS
    assert "no_source_window: false" in queries.ADMIT_CENSUS
    assert "CONFIRM_OBSERVED_PUBLICATION" not in queries.CONFIRM_OBSERVED_PUBLICATION
    assert "publication.publication_id = $publication_id" in queries.RESERVE_PUBLICATION
    assert "payload_digest" in queries.RESERVE_PUBLICATION
    assert "AUTHORIZE_PUBLICATION_BROKER" in queries.__dict__
    assert "pre_broker_authorized_at" in queries.AUTHORIZE_PUBLICATION_BROKER
    assert "attempt.deadline_at > datetime()" in queries.CHECKPOINT_UNIT
    assert "company_binding_after_contact_id" in queries.CHECKPOINT_UNIT
    assert "RENEW_UNIT_FENCE" not in queries.RENEW_UNIT_FENCE
    assert "ready273:DataMigration" in queries.RELEASE_UNIT_FENCE
    assert "current_generation" in queries.RECORD_HTTP_OUTCOME


def test_settlement_queries_do_not_require_live_source_control_authority() -> None:
    """Stale authority blocks external work but not durable retirement/finalization."""
    for query in (
        queries.REQUEST_CANCELLATION,
        queries.MARK_PUBLICATION_AMBIGUOUS,
        queries.CONFIRM_OBSERVED_PUBLICATION,
        queries.SETTLE_UNIT,
        queries.RELEASE_UNIT_FENCE,
        queries.TERMINALIZE_CENSUS,
    ):
        assert "status: 'active'" not in query
    assert "status: 'active'" in queries.RESERVE_PUBLICATION
    assert "status: 'active'" in queries.AUTHORIZE_PUBLICATION_BROKER
    assert "MARK_CENSUS_AUTHORITY_STALE" in queries.__dict__


def test_fence_identity_contract_is_generation_scoped() -> None:
    assert (
        "standalone_crm_census_fence_unique",
        "StandaloneCrmUnitFence",
        ("census_id", "generation", "unit_kind"),
    ) in CENSUS_CONSTRAINT_SPECS


def test_census_neo4j_fixture_installs_only_272_migration_prerequisites() -> None:
    """Keep #273 readiness coverage independent of test-suite ordering."""
    assert any(
        "data_migration_key_unique" in statement for statement in CONTROL_MIGRATION_BASE_CONSTRAINTS
    )
    assert all(
        statement in CONTROL_MIGRATION_BASE_CONSTRAINTS
        for statement in CREATE_BITRIX_SOURCE_INSTANCE_CONSTRAINTS
    )
    census_schema = {
        *queries.CREATE_STANDALONE_CRM_CENSUS_CONSTRAINTS,
        *queries.CREATE_STANDALONE_CRM_CENSUS_INDEXES,
    }
    assert census_schema.isdisjoint(CONTROL_MIGRATION_BASE_CONSTRAINTS)


def test_census_tasks_are_routed_but_never_scheduled_by_beat() -> None:
    assert celery_app.conf.task_routes["src.standalone_crm_census_tasks.*"] == {
        "queue": "ingestion"
    }
    assert all(
        "standalone_crm_census" not in str(entry["task"]) for entry in _beat_schedule.values()
    )


def test_cypher_branch_guards_are_neo4j_5_valid_and_reservation_locks_are_nonmutating() -> None:
    for query in (queries.CLAIM_ATTEMPT, queries.CONTINUE_ATTEMPT, queries.RESERVE_PUBLICATION):
        assert "WITH census, redelivery\n  WHERE" not in query
        assert "WITH census, unit, existing_rows, has_identity_conflict\n  WHERE" not in query
        assert "UNWIND CASE WHEN" in query
    for query in (queries.RESERVE_HTTP_CALL, queries.RESERVE_PUBLICATION):
        assert "SET census.updated_at = census.updated_at" in query
        assert "SET census.updated_at = datetime()\nWITH census, attempt" not in query


def test_attempt_lifecycle_query_contracts_reject_competition_and_require_recovery() -> None:
    assert "size(redelivery) = 1" in queries.CLAIM_ATTEMPT
    assert (
        "current.state IN ['paused_with_checkpoint','superseded','failed','completed']"
        in queries.CLAIM_ATTEMPT
    )
    assert "old.lease_until < datetime()" in queries.RECOVER_ATTEMPT
    assert "old.state = 'superseded'" in queries.RECOVER_ATTEMPT
    assert "fence.state = 'superseded'" in queries.RECOVER_ATTEMPT
    assert "census.attempt_count < $max_attempts" in queries.CONTINUE_ATTEMPT
    assert "census.cancel_requested_at IS NULL" in queries.CONTINUE_ATTEMPT
    assert "FAIL_EXHAUSTED_CENSUS" not in queries.FAIL_EXHAUSTED_CENSUS
    assert "terminal_state = 'failed'" in queries.FAIL_EXHAUSTED_CENSUS


def test_cancellation_and_terminal_derivation_query_contracts() -> None:
    assert (
        "WHEN pre_window THEN 'freeze_failed' ELSE 'cancel_requested'"
        in queries.REQUEST_CANCELLATION
    )
    assert "REMOVE scope.active_census_id" in queries.REQUEST_CANCELLATION
    assert (
        "WHEN failed > 0 OR any(unit IN units WHERE unit.state = 'failed') THEN 'failed'"
        in queries.TERMINALIZE_CENSUS
    )
    assert (
        "WHEN census.cancel_requested_at IS NOT NULL THEN 'cancelled_with_checkpoint'"
        in queries.TERMINALIZE_CENSUS
    )
    assert (
        "all(unit IN units WHERE unit.state IN ['completed','failed','cancelled','superseded'])"
        in (queries.TERMINALIZE_CENSUS)
    )
    assert "processed + skipped + failed + no_work = size(units)" not in queries.TERMINALIZE_CENSUS
    assert "publication.status = 'published'" in queries.TERMINALIZE_CENSUS
    assert "fence.state IN ['released','superseded']" in queries.TERMINALIZE_CENSUS
    assert "expected_unit_count" in queries.TERMINALIZE_CENSUS


def test_checkpoint_contract_is_fenced_monotonic_and_budgeted() -> None:
    assert "current_generation: $generation" in queries.CHECKPOINT_UNIT
    assert "checkpoint.last_committed_id" in queries.CHECKPOINT_UNIT
    assert "company_binding_after_contact_id" in queries.CHECKPOINT_UNIT
    assert "attempt.deadline_at > datetime()" in queries.CHECKPOINT_UNIT
    assert "$max_rows_per_attempt" in queries.CHECKPOINT_UNIT
    assert "$max_rows_per_occurrence" in queries.CHECKPOINT_UNIT
    assert "attempt.row_count +" in queries.CHECKPOINT_UNIT
    assert "census.row_count +" in queries.CHECKPOINT_UNIT
    assert "attempt.row_count = attempt.row_count +" in queries.CHECKPOINT_UNIT


def test_checkpoint_callback_contract_validates_before_work_and_commits_after_work() -> None:
    assert "VALIDATE_CHECKPOINT_UNIT" in queries.__dict__
    assert "checkpoint.version" in queries.VALIDATE_CHECKPOINT_UNIT
    assert "SET census.row_count" not in queries.VALIDATE_CHECKPOINT_UNIT
    assert "SET census.row_count" in queries.CHECKPOINT_UNIT
    assert "fence.owner_task_id = $task_id" in queries.CLAIM_UNIT_FENCE
    assert "$recovery = true" in queries.CLAIM_UNIT_FENCE
    assert "generation: $generation" in queries.CLAIM_UNIT_FENCE
    assert "state: 'active'" in queries.RENEW_UNIT_FENCE
    assert "fence.cancel_requested_at" in queries.REQUEST_CANCELLATION


@pytest.mark.parametrize("observation", ["fence_claim", "checkpoint_advanced"])
def test_meaningful_child_observation_confirms_without_broker_io(observation: str) -> None:
    repository = _Repository(observed=observation)
    publisher = _Publisher(False)
    _runtime(repository, publisher).repair_publication("publication")
    assert repository.confirmed == 1
    assert repository.publishing == 0
    assert publisher.calls == []


def test_allocated_checkpoint_is_not_observed_child_evidence() -> None:
    repository = _Repository(observed="none")
    publisher = _Publisher(True)
    _runtime(repository, publisher).repair_publication("publication")
    assert repository.confirmed == 1
    assert repository.publishing == 1
    assert len(publisher.calls) == 1


def test_corrected_query_contracts_keep_tokens_and_publications_exact() -> None:
    assert "checkpoint.version > 1" in queries.GET_PUBLICATION_RECOVERY
    assert "checkpoint.child_fence_token > 0" in queries.GET_PUBLICATION_RECOVERY
    assert "count(checkpoint) > 0 AS observed_child" not in queries.GET_PUBLICATION_RECOVERY
    assert "publication.payload_json = $payload_json" in queries.RESERVE_PUBLICATION
    assert "publication.task_id = $task_id" in queries.RESERVE_PUBLICATION
    assert "publication.task_name = $task_name" in queries.RESERVE_PUBLICATION
    assert "publication.queue = $queue" in queries.RESERVE_PUBLICATION
    assert "payload_conflict" in queries.RESERVE_PUBLICATION
    assert "OPTIONAL MATCH (existing:StandaloneCrmChildPublication" in queries.RESERVE_PUBLICATION
    assert "unit.state IN ['pending_publication','paused']" in queries.RESERVE_PUBLICATION
    assert "MERGE (publication:StandaloneCrmChildPublication" in queries.RESERVE_PUBLICATION
    assert "$parent_fence_token" in queries.CLAIM_UNIT_FENCE
    assert "$child_fence_token" in queries.CHECKPOINT_UNIT
    assert "$child_task_id" in queries.CHECKPOINT_UNIT
    assert "attempt.lease_until >= datetime()" in queries.RELEASE_UNIT_FENCE
    assert "fence.cancel_requested_at IS NULL" in queries.RENEW_UNIT_FENCE
    assert "census.cancel_requested_by = coalesce" in queries.REQUEST_CANCELLATION
    assert "first_request" in queries.REQUEST_CANCELLATION
    assert "publication.status IN ['published','retired']" in queries.TERMINALIZE_CENSUS
    assert "publication.generation = unit.generation" in queries.TERMINALIZE_CENSUS
    assert "unit.upper_id = 0" in queries.TERMINALIZE_CENSUS
    assert "publication.sequence = 1" in queries.TERMINALIZE_CENSUS


def test_terminal_publication_predicates_distinguish_completion_and_cancellation() -> None:
    terminal = queries.TERMINALIZE_CENSUS
    assert "publication.status IN ['published','retired']" in terminal
    assert "publication.status = 'published'" in terminal
    assert "publication.status = 'retired'" in terminal
    assert "unit.state = 'cancelled'" in terminal
    assert "checkpoint.version > 1" in terminal
    assert "checkpoint.child_fence_token > 0]) = 0" in terminal
    assert "census.cancel_requested_at IS NULL" in queries.MARK_PUBLICATION_PUBLISHING
    assert "census.cancel_requested_at IS NULL" in queries.MARK_PUBLICATION_PUBLISHED
    assert "size([fence IN fences WHERE fence.unit_kind = unit.unit_kind]) = 0" in terminal


def test_settlement_query_contract_is_fenced_monotonic_and_atomic() -> None:
    assert "VALIDATE_SETTLE_UNIT" in queries.__dict__
    assert "SETTLE_UNIT" in queries.__dict__
    assert "unit.state = $terminal_state" in queries.SETTLE_UNIT
    assert "fence.state = 'released'" in queries.SETTLE_UNIT
    assert "owner_task_id: $child_task_id" in queries.SETTLE_UNIT
    assert "census.cancel_requested_at IS NOT NULL" in queries.VALIDATE_SETTLE_UNIT
    assert "$terminal_state = 'cancelled'" in queries.VALIDATE_SETTLE_UNIT
    assert "publication.status = 'retired'" in queries.REQUEST_CANCELLATION


def test_repair_revalidates_authority_before_any_broker_work() -> None:
    class StaleAuthority(_Authority):
        def source_sync_heads(self, request: object) -> SourceSyncAuthoritySnapshot:
            del request
            return SourceSyncAuthoritySnapshot("changed", "digest", None)

    repository = _Repository()
    publisher = _Publisher(True)
    runtime = StandaloneCrmCensusRuntime(
        repository=repository,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=StaleAuthority(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.repair_publication("publication")
    assert publisher.calls == []
    assert repository.publishing == 0


def test_reconcile_revalidates_before_terminal_mutation() -> None:
    class StaleAuthority(_Authority):
        def source_sync_heads(self, request: object) -> SourceSyncAuthoritySnapshot:
            del request
            return SourceSyncAuthoritySnapshot("changed", "digest", None)

    repository = _Repository()
    publisher = _Publisher(True)
    runtime = StandaloneCrmCensusRuntime(
        repository=repository,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=StaleAuthority(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    assert runtime.reconcile("census") == ("completed", 1)
    assert repository.terminalized == 1
    assert repository.stale_marked == 1


def test_repair_marks_reserved_publication_ambiguous_when_authority_changes_before_broker_io() -> (
    None
):
    class ChangesAfterInitialValidation(_Authority):
        def __init__(self) -> None:
            self.calls = 0

        def source_sync_heads(self, request: object) -> SourceSyncAuthoritySnapshot:
            del request
            self.calls += 1
            mapping_head = "mapping" if self.calls == 1 else "changed"
            return SourceSyncAuthoritySnapshot(mapping_head, "digest", None)

    repository = _Repository()
    publisher = _Publisher(True)
    runtime = StandaloneCrmCensusRuntime(
        repository=repository,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=ChangesAfterInitialValidation(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.repair_publication("publication")
    assert publisher.calls == []
    assert repository.publishing == 0
    assert repository.ambiguous == 1
    assert repository.confirmed == 0


def test_continuation_and_unknown_call_queries_are_generation_fenced() -> None:
    assert (
        "census.state IN ['paused_with_checkpoint','recovering','running']"
        in queries.CONTINUE_ATTEMPT
    )
    assert "old.state IN ['paused_with_checkpoint','superseded']" in queries.CONTINUE_ATTEMPT
    assert "old.state = 'superseded'" in queries.CONTINUE_ATTEMPT
    assert "size(redelivery) = 1 AND census.state = 'running'" in queries.CONTINUE_ATTEMPT
    assert "redelivery[0].state = 'running'" in queries.CLAIM_ATTEMPT
    assert (
        "old_publication.status IN ['reserved','publishing','ambiguous']"
        in queries.CONTINUE_ATTEMPT
    )
    assert "StandaloneCrmHttpCallReservation" in queries.CONTINUE_ATTEMPT
    assert "outcome: 'reserved'" in queries.RECOVER_ATTEMPT
    assert "outcome: 'reserved'" in queries.CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN
    assert "census.current_generation" not in queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN
    assert "outcome: 'reserved'" in queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN
    assert (
        "generation: census.current_generation"
        not in queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN
    )
    assert "fingerprint: $fingerprint" in queries.CLASSIFY_CURRENT_RESERVED_HTTP_CALL_UNKNOWN
    assert "reservation.outcome = 'unknown'" in queries.CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN
    assert "current_generation: $generation" in queries.CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN
    assert "fingerprint: $fingerprint" in queries.CLASSIFY_RESERVED_HTTP_CALL_UNKNOWN


def test_terminal_query_allows_only_settled_historical_publication_generations() -> None:
    terminal = queries.TERMINALIZE_CENSUS
    assert "publication.generation = unit.generation" in terminal
    assert "fence.generation = publication.generation" in terminal
    assert "publication.status IN ['published','retired']" in terminal
    assert "publication.generation = $generation" not in terminal


def test_publication_id_is_globally_unique_and_reservation_conflicts_before_merge() -> None:
    assert (
        "standalone_crm_census_publication_id_unique",
        "StandaloneCrmChildPublication",
        ("publication_id",),
    ) in CENSUS_CONSTRAINT_SPECS
    assert (
        "OPTIONAL MATCH (by_id:StandaloneCrmChildPublication {publication_id: $publication_id})"
        in queries.RESERVE_PUBLICATION
    )
    assert (
        "identity_conflict OR NOT immutable_match AS payload_conflict"
        in queries.RESERVE_PUBLICATION
    )


def test_stale_authority_is_pending_until_settlement_and_never_counts_as_a_row() -> None:
    assert "census.state = 'authority_stale_pending'" in queries.MARK_CENSUS_AUTHORITY_STALE
    assert "terminal_state = 'failed'" not in queries.MARK_CENSUS_AUTHORITY_STALE
    assert "terminal_at" not in queries.MARK_CENSUS_AUTHORITY_STALE
    assert "WHEN fatal_stale THEN 'authority_stale_pending'" in queries.REQUEST_CANCELLATION
    assert "pre_window AND NOT fatal_stale" in queries.REQUEST_CANCELLATION
    assert (
        "WHEN census.fatal_reason = 'authority_stale' THEN 'failed'" in queries.TERMINALIZE_CENSUS
    )
    assert (
        "$processed_count + $skipped_count + $failed_count + $no_work_count"
        not in queries.CHECKPOINT_UNIT
    )
    assert "checkpoint.no_work_count = $no_work_count" in queries.CHECKPOINT_UNIT


def test_expired_owner_can_only_settle_existing_checkpoint_after_cancel_or_fatal_stale() -> None:
    settle = queries.VALIDATE_SETTLE_UNIT
    assert "census.fatal_reason = 'authority_stale' AND $terminal_state = 'failed'" in settle
    assert "$terminal_state = 'cancelled'" in settle
    assert "$processed_count = checkpoint.processed_count" in settle
    assert "attempt.deadline_at > datetime()" in queries.CHECKPOINT_UNIT
    assert "attempt.lease_until = datetime()" in queries.RENEW_UNIT_FENCE


def test_observed_repair_uses_settlement_authority_before_live_revalidation() -> None:
    class StaleAuthority(_Authority):
        def source_sync_heads(self, request: object) -> SourceSyncAuthoritySnapshot:
            del request
            raise RuntimeError("authority changed")

    repository = _Repository(observed="checkpoint_advanced")
    publisher = _Publisher(False)
    runtime = StandaloneCrmCensusRuntime(
        repository=cast(StandaloneCrmCensusRepository, repository),
        admission=_Admission(),
        authority=StaleAuthority(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    runtime.repair_publication("publication")
    assert repository.confirmed == 1
    assert repository.stale_marked == 0
    assert publisher.calls == []


def test_already_published_observed_repair_is_a_noop_without_revalidation() -> None:
    repository = _Repository(observed="fence_claim")
    repository.publication = StandaloneCrmPublication(
        "publication",
        "task",
        '{"generation":1,"unit_kind":"contact"}',
        "sha256:payload",
        "child",
        "ingestion",
        "published",
    )
    publisher = _Publisher(False)
    _runtime(repository, publisher).repair_publication("publication")
    assert repository.confirmed == 0
    assert publisher.calls == []
