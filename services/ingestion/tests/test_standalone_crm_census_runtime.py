from __future__ import annotations

from dataclasses import dataclass

import pytest
from src.graph.standalone_crm_census import StandaloneCrmPublicationRepair
from src.ingestion_config import BitrixOpenLinesConfig
from src.standalone_crm_census_models import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
)
from src.standalone_crm_census_runtime import StandaloneCrmCensusRuntime
from src.standalone_crm_census_types import StandaloneCrmReason


@dataclass(frozen=True)
class _Snapshot:
    request: SourceSyncCensusRequest
    generation: int
    state: str
    cancel_requested: bool = False
    window_frozen: bool = False


class _Authority:
    def __init__(self, *, stale: bool = False) -> None:
        self.calls = 0
        self._stale = stale

    def verify(self, request: SourceSyncCensusRequest) -> None:
        del request
        self.calls += 1
        if self._stale:
            from src.standalone_crm_census_types import StandaloneCrmCensusAuthorityError

            raise StandaloneCrmCensusAuthorityError("authority changed")


class _Repository:
    def __init__(self, request: SourceSyncCensusRequest) -> None:
        self.snapshot = _Snapshot(request, 0, "allocated")
        self.frozen: object | None = None
        self.units: tuple[object, ...] = ()
        self.terminal: tuple[object, ...] | None = None
        self.resumed: list[str] = []

    def require_active_source(self, request: SourceSyncCensusRequest) -> None:
        assert request.source_key == "bitrix_chat"

    def runtime_snapshot(self, census_id: str) -> _Snapshot | None:
        return self.snapshot if census_id == "census" else None

    def claim_attempt(
        self, census_id: str, generation: int, fence_token: int, request: SourceSyncCensusRequest
    ) -> bool:
        assert request.occurrence_key == "occurrence"
        assert (census_id, generation, fence_token) == ("census", 1, 1)
        return True

    def freeze_source_window(self, census_id: str, generation: int, window: object) -> bool:
        assert (census_id, generation) == ("census", 1)
        self.frozen = window
        return True

    def allocate_units(self, census_id: str, generation: int, units: tuple[object, ...]) -> int:
        assert (census_id, generation) == ("census", 1)
        self.units = units
        return len(units)

    def terminalize(self, *values: object) -> bool:
        self.terminal = values
        return True

    def fail_after_window_authority(
        self, census_id: str, generation: int, reason: StandaloneCrmReason
    ) -> bool:
        assert (census_id, generation, reason.code) == ("census", 1, "authority_stale")
        return True

    def settle_attempt(self, census_id: str, generation: int) -> bool:
        assert (census_id, generation) == ("census", 1)
        return True

    def recover_or_take_over_attempt(self, *_: object) -> None:
        return None

    def create_continuation(self, *_: object) -> int | None:
        raise AssertionError("pre-window recovery must not create a second generation")

    def resume(self, census_id: str) -> bool:
        self.resumed.append(census_id)
        return True

    def pause(self, census_id: str, generation: int, reason_code: str, detail: str) -> bool:
        assert census_id == "census"
        assert generation >= 0
        assert reason_code
        assert detail
        return True


class _PostWindowCancellationRepository(_Repository):
    def __init__(self, request: SourceSyncCensusRequest) -> None:
        super().__init__(request)
        self.snapshot = _Snapshot(request, 1, "running", cancel_requested=True, window_frozen=True)
        self.settled_cancellations: list[tuple[str, int]] = []
        self.failed_freezes: list[tuple[str, int, StandaloneCrmReason]] = []

    def settle_cancellation(self, census_id: str, generation: int) -> bool:
        self.settled_cancellations.append((census_id, generation))
        return True

    def fail_freeze(self, census_id: str, generation: int, reason: StandaloneCrmReason) -> bool:
        self.failed_freezes.append((census_id, generation, reason))
        return True


class _AuthorityFailureRepository(_Repository):
    def __init__(self, request: SourceSyncCensusRequest, *, window_frozen: bool) -> None:
        super().__init__(request)
        self.snapshot = _Snapshot(
            request, 1 if window_frozen else 0, "running", window_frozen=window_frozen
        )
        self.failed_freezes: list[tuple[str, int, StandaloneCrmReason]] = []
        self.post_window_failures: list[tuple[str, int, StandaloneCrmReason]] = []

    def fail_freeze(self, census_id: str, generation: int, reason: StandaloneCrmReason) -> bool:
        self.failed_freezes.append((census_id, generation, reason))
        return True

    def fail_after_window_authority(
        self, census_id: str, generation: int, reason: StandaloneCrmReason
    ) -> bool:
        self.post_window_failures.append((census_id, generation, reason))
        return True


class _ReconciliationRepository(_Repository):
    def __init__(self, request: SourceSyncCensusRequest, terminal_results: list[bool]) -> None:
        super().__init__(request)
        self.snapshot = _Snapshot(request, 1, "running", window_frozen=True)
        self._terminal_results = terminal_results
        self.settled_attempts: list[tuple[str, int]] = []
        self.terminal_states: list[str] = []

    def classify_unresolved_calls(self, census_id: str) -> int:
        assert census_id == "census"
        return 2

    def converge_limit_denial(
        self, census_id: str, generation: int, request: SourceSyncCensusRequest, reason_code: str
    ) -> str | None:
        assert (census_id, generation, request.occurrence_key, reason_code) == (
            "census",
            1,
            "occurrence",
            "attempts_exhausted",
        )
        return None

    def repair_publications(self, census_id: str) -> tuple[StandaloneCrmPublicationRepair, ...]:
        assert census_id == "census"
        return ()

    def settle_attempt(self, census_id: str, generation: int) -> bool:
        self.settled_attempts.append((census_id, generation))
        return True

    def terminalize(self, *values: object) -> bool:
        terminal_state = values[2]
        assert isinstance(terminal_state, str)
        self.terminal_states.append(terminal_state)
        return self._terminal_results.pop(0)


class _LimitConvergenceRepository(_Repository):
    def __init__(self, request: SourceSyncCensusRequest, state: str) -> None:
        super().__init__(request)
        self.snapshot = _Snapshot(request, 0, "allocated")
        self.state = state
        self.claims: list[tuple[str, int, int]] = []
        self.convergences: list[tuple[str, int, str]] = []

    def claim_attempt(
        self, census_id: str, generation: int, fence_token: int, request: SourceSyncCensusRequest
    ) -> bool:
        assert request.occurrence_key == "occurrence"
        self.claims.append((census_id, generation, fence_token))
        return False

    def converge_limit_denial(
        self, census_id: str, generation: int, request: SourceSyncCensusRequest, reason_code: str
    ) -> str | None:
        assert request.occurrence_key == "occurrence"
        self.convergences.append((census_id, generation, reason_code))
        return self.state


class _Probe:
    def __init__(self, bounds: dict[str, int]) -> None:
        self._bounds = bounds
        self.calls: list[str] = []

    def upper_bound(self, stream_kind: str) -> int:
        self.calls.append(stream_kind)
        return self._bounds[stream_kind]


class _Publisher:
    def __init__(self, *, handler: bool = True, raises: bool = False) -> None:
        self.handler = handler
        self.raises = raises
        self.calls: list[tuple[str, str, str, str]] = []

    def has_handler(self, task_name: str) -> bool:
        assert task_name == "child.task"
        return self.handler

    def publish(self, task_name: str, task_id: str, queue: str, payload_json: str) -> None:
        self.calls.append((task_name, task_id, queue, payload_json))
        if self.raises:
            raise RuntimeError("broker unavailable")


class _RepairRepository(_Repository):
    def __init__(self, request: SourceSyncCensusRequest, *, confirmed: bool = True) -> None:
        super().__init__(request)
        self.confirmed = confirmed
        self.marked: list[object] = []
        self.confirmed_publications: list[object] = []

    def repair_publications(self, census_id: str) -> tuple[StandaloneCrmPublicationRepair, ...]:
        assert census_id == "census"
        return (
            StandaloneCrmPublicationRepair(
                "task-id",
                "publishing",
                '{"stable":true}',
                "child.task",
                "ingestion",
                "sha256:" + "d" * 64,
                "contact",
                1,
            ),
        )

    def mark_publication_publishing(self, publication: object) -> str | None:
        self.marked.append(publication)
        return '{"stable":true}'

    def confirm_publication(self, publication: object) -> bool:
        self.confirmed_publications.append(publication)
        return self.confirmed


def _request(kinds: tuple[str, ...] = ("contact", "lead")) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "source",
        "control",
        "occurrence",
        kinds,
        StandaloneCrmBudget(2, 2, 20, 4, 4, 3, "2099-01-02T00:00:00Z"),
        "policy",
        "association",
        "sha256:" + "a" * 64,
        SourceSyncAuthority("mapping", "sha256:" + "b" * 64, "projection", "sha256:" + "c" * 64),
    )


def test_runtime_freezes_all_selected_bounds_before_allocating_units() -> None:
    request = _request()
    repository = _Repository(request)
    authority = _Authority()
    probe = _Probe({"contact": 3, "lead": 8})
    runtime = StandaloneCrmCensusRuntime(
        repository,
        authority,
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        probe=probe,
    )

    result = runtime.run_parent("census", request)

    assert result.state == "paused_with_checkpoint"
    assert probe.calls == ["contact", "lead"]
    assert repository.frozen is not None
    assert len(repository.units) == 2
    assert authority.calls >= 6


def test_runtime_probes_all_streams_in_canonical_crm_order() -> None:
    request = _request(("company", "lead", "contact"))
    repository = _Repository(request)
    probe = _Probe({"contact": 3, "lead": 8, "company": 11})
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        probe=probe,
    )

    runtime.run_parent("census", request)

    assert probe.calls == ["contact", "lead", "company"]


def test_runtime_all_zero_window_terminalizes_without_publisher() -> None:
    request = _request(("contact",))
    repository = _Repository(request)
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        probe=_Probe({"contact": 0}),
    )

    result = runtime.run_parent("census", request)

    assert result.state == "completed"
    assert repository.terminal is not None
    reason = repository.terminal[3]
    assert isinstance(reason, StandaloneCrmReason)
    assert reason.code == "completed"


def test_pre_window_resume_refreezes_bounded_window_in_same_generation() -> None:
    request = _request(("contact",))
    repository = _Repository(request)
    repository.snapshot = _Snapshot(request, 1, "paused_with_checkpoint")
    probe = _Probe({"contact": 0})
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        probe=probe,
    )

    result = runtime.continue_after_pause("census")

    assert result.state == "completed"
    assert result.generation == 1
    assert repository.resumed == ["census"]
    assert probe.calls == ["contact"]
    assert repository.frozen is not None
    assert len(repository.units) == 1
    assert repository.terminal is not None


def test_redelivered_parent_settles_post_window_cancellation_with_checkpoint() -> None:
    request = _request(("contact",))
    repository = _PostWindowCancellationRepository(request)
    probe = _Probe({"contact": 9})
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        probe=probe,
    )

    result = runtime.run_parent("census", request)

    assert result.state == "cancelled_with_checkpoint"
    assert result.generation == 1
    assert repository.settled_cancellations == [("census", 1)]
    assert repository.failed_freezes == []
    assert probe.calls == []
    assert repository.terminal is not None
    assert repository.terminal[2] == "cancelled_with_checkpoint"
    reason = repository.terminal[3]
    assert isinstance(reason, StandaloneCrmReason)
    assert reason.code == "cancelled"


def test_stale_authority_parent_redelivery_converges_before_window_without_claiming() -> None:
    request = _request(("contact",))
    repository = _AuthorityFailureRepository(request, window_frozen=False)
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(stale=True),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
    )

    result = runtime.run_parent("census", request)

    assert result.state == "freeze_failed"
    assert repository.failed_freezes == [
        ("census", 0, StandaloneCrmReason("authority_stale", "authority changed before window"))
    ]


def test_stale_authority_after_window_fails_before_cancellation_settlement() -> None:
    request = _request(("contact",))
    repository = _AuthorityFailureRepository(request, window_frozen=True)
    repository.snapshot = _Snapshot(
        request, 1, "running", cancel_requested=True, window_frozen=True
    )
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(stale=True),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
    )

    result = runtime.run_parent("census", request)

    assert result.state == "failed"
    assert repository.post_window_failures == [
        ("census", 1, StandaloneCrmReason("authority_stale", "authority changed after window"))
    ]
    assert repository.terminal is None


def test_reconcile_settles_the_attempt_then_selects_completed_or_failed_terminal_state() -> None:
    request = _request(("contact",))
    completed_repository = _ReconciliationRepository(request, [True])
    failed_repository = _ReconciliationRepository(request, [False, True])
    config = BitrixOpenLinesConfig(standalone_crm_identity_enabled=True)

    completed = StandaloneCrmCensusRuntime(completed_repository, _Authority(), config).reconcile(
        "census"
    )
    failed = StandaloneCrmCensusRuntime(failed_repository, _Authority(), config).reconcile("census")

    assert completed.state == "completed"
    assert completed_repository.settled_attempts == [("census", 1)]
    assert completed_repository.terminal_states == ["completed"]
    assert failed.state == "failed"
    assert failed_repository.settled_attempts == [("census", 1)]
    assert failed_repository.terminal_states == ["completed", "failed"]


@pytest.mark.parametrize(
    ("deadline", "expected_reason"),
    [
        ("2099-01-01T00:00:00Z", "attempts_exhausted"),
        ("2000-01-01T00:00:00Z", "deadline_exhausted"),
    ],
)
def test_claim_denial_converges_attempt_or_deadline_exhaustion(
    deadline: str, expected_reason: str
) -> None:
    request = _request(("contact",))
    request = SourceSyncCensusRequest(
        request.source_key,
        request.source_instance_id,
        request.control_instance_id,
        request.occurrence_key,
        request.selected_kinds,
        StandaloneCrmBudget(2, 2, 20, 4, 4, 3, deadline),
        request.policy_version,
        request.association_contract_version,
        request.configuration_digest,
        request.authority,
    )
    repository = _LimitConvergenceRepository(request, "failed")
    runtime = StandaloneCrmCensusRuntime(
        repository, _Authority(), BitrixOpenLinesConfig(standalone_crm_identity_enabled=True)
    )

    result = runtime.run_parent("census", request)

    assert result.state == "failed"
    assert repository.claims == [("census", 1, 1)]
    assert repository.convergences == [("census", 0, expected_reason)]


def test_runtime_is_disabled_before_claim_or_probe() -> None:
    request = _request(("contact",))
    repository = _Repository(request)
    runtime = StandaloneCrmCensusRuntime(
        repository, _Authority(), BitrixOpenLinesConfig(standalone_crm_identity_enabled=False)
    )

    try:
        runtime.run_parent("census", request)
    except RuntimeError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("disabled control must fail closed")


def test_repair_republishes_exact_stored_task_identity_after_acknowledgement_loss() -> None:
    request = _request(("contact",))
    repository = _RepairRepository(request)
    publisher = _Publisher()
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        publisher=publisher,
    )

    result = runtime.repair_publications("census")

    assert result.state == "running"
    assert publisher.calls == [("child.task", "task-id", "ingestion", '{"stable":true}')]
    assert len(repository.marked) == 1
    assert len(repository.confirmed_publications) == 1


def test_repair_leaves_publishing_for_broker_or_confirmation_failure() -> None:
    request = _request(("contact",))
    broker_repository = _RepairRepository(request)
    broker_runtime = StandaloneCrmCensusRuntime(
        broker_repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        publisher=_Publisher(raises=True),
    )
    confirmation_repository = _RepairRepository(request, confirmed=False)
    confirmation_runtime = StandaloneCrmCensusRuntime(
        confirmation_repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        publisher=_Publisher(),
    )

    assert broker_runtime.repair_publications("census").state == "publishing"
    assert confirmation_runtime.repair_publications("census").state == "publishing"
    assert len(broker_repository.confirmed_publications) == 0
    assert len(confirmation_repository.confirmed_publications) == 1


def test_repair_does_not_publish_when_child_handler_is_absent() -> None:
    request = _request(("contact",))
    repository = _RepairRepository(request)
    publisher = _Publisher(handler=False)
    runtime = StandaloneCrmCensusRuntime(
        repository,
        _Authority(),
        BitrixOpenLinesConfig(standalone_crm_identity_enabled=True),
        publisher=publisher,
    )

    result = runtime.repair_publications("census")

    assert result.state == "paused_with_checkpoint"
    assert publisher.calls == []


def test_mapping_publication_selects_dedicated_zero_bitrix_task() -> None:
    from src.standalone_crm_census_runtime import MAPPING_CHILD_TASK_NAME

    assert MAPPING_CHILD_TASK_NAME == (
        "src.standalone_crm_census_tasks.run_standalone_crm_mapping_activation"
    )


def test_mapping_freeze_uses_legacy_target_and_v2_rollback_head_work_identity() -> None:
    from src.standalone_crm_census_models import MappingRollbackCensusRequest
    from src.standalone_crm_census_requests import MappingRollbackAuthority

    class MappingRepository(_Repository):
        def __init__(self, request: MappingRollbackCensusRequest) -> None:
            del request
            self.frozen: object | None = None

        def freeze_no_source_window(self, _census: str, _generation: int, window: object) -> bool:
            self.frozen = window
            return True

        def allocate_units(self, _census: str, _generation: int, units: tuple[object, ...]) -> int:
            return len(units)

        def pause(self, *_: object) -> bool:
            return True

    budget = StandaloneCrmBudget(1, 1, 1, 1, 1, 1, "2099-01-02T00:00:00Z")
    rollback_digest = "sha256:" + "c" * 64
    for authority, expected in (
        (
            MappingRollbackAuthority("target", "target-digest", "head", "rollback"),
            ("target", "target-digest"),
        ),
        (
            MappingRollbackAuthority(
                "target",
                "target-digest",
                "head",
                "rollback",
                rollback_digest,
                "release",
                "sha256:" + "a" * 64,
                None,
                None,
                None,
                "projection",
                None,
                None,
                None,
            ),
            ("rollback", rollback_digest),
        ),
    ):
        request = MappingRollbackCensusRequest(
            "bitrix_chat",
            "source",
            "control",
            "mapping",
            ("lead",),
            budget,
            "policy",
            "association",
            "sha256:" + "b" * 64,
            authority,
        )
        repository = MappingRepository(request)
        runtime = StandaloneCrmCensusRuntime(
            repository, _Authority(), BitrixOpenLinesConfig(standalone_crm_identity_enabled=True)
        )
        runtime._freeze_mapping("census", request, 1)
        assert repository.frozen is not None
        assert repository.frozen.revision_id == expected[0]
        assert repository.frozen.revision_digest == expected[1]
