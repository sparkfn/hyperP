"""Fake-only runtime matrix for bounded source and mapping census behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStatus,
)
from src.standalone_crm_census_models import (
    StandaloneCrmAttempt,
    StandaloneCrmBudgetSnapshot,
    StandaloneCrmTerminalAccounting,
)
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncAuthoritySnapshot,
    SourceSyncCensusRequest,
)
from src.standalone_crm_census_runtime import (
    StandaloneCrmAuthorityReader,
    StandaloneCrmCensusRuntime,
)


def _budget() -> StandaloneCrmBudgetSnapshot:
    return StandaloneCrmBudgetSnapshot(10, 10, 60.0, 20, 20, 3, 120.0)


def _source(kinds: tuple[str, ...]) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "source",
        "control",
        "occurrence",
        "operator",
        kinds,
        "policy",
        "association",
        "digest",
        _budget(),
    )  # type: ignore[arg-type]


class _Authority:
    def source_sync_heads(self, request: SourceSyncCensusRequest) -> SourceSyncAuthoritySnapshot:
        del request
        return SourceSyncAuthoritySnapshot("mapping", "digest", None)

    def validate_mapping_prepare(self, request: MappingPrepareCensusRequest) -> None:
        del request

    def validate_mapping_rollback(self, request: MappingRollbackCensusRequest) -> None:
        del request


class _Admission:
    def admit(self, *, control_instance_id: str, source_instance_id: str) -> None:
        assert (control_instance_id, source_instance_id) == ("control", "source")


class _Client:
    def __init__(self, bounds: dict[str, int], fail: bool = False) -> None:
        self.bounds = bounds
        self.fail = fail
        self.closed = False

    def probe_crm_contact_upper_id(self) -> int:
        return self._value("contact")

    def probe_crm_lead_upper_id(self) -> int:
        return self._value("lead")

    def probe_crm_company_upper_id(self) -> int:
        return self._value("company")

    def _value(self, kind: str) -> int:
        if self.fail:
            raise RuntimeError("probe failed")
        return self.bounds[kind]

    def close(self) -> None:
        self.closed = True


class _SelectivePublisher:
    def __init__(self, unavailable: set[str]) -> None:
        self._unavailable = unavailable
        self.checked: list[str] = []
        self.published: list[str] = []

    def handler_available(self, census_kind: str, unit_kind: str) -> bool:
        del census_kind
        self.checked.append(unit_kind)
        return unit_kind not in self._unavailable

    def publish(self, *, task_name: str, task_id: str, queue: str, payload_json: str) -> None:
        del task_name, task_id, queue
        self.published.append(payload_json)


class _Publisher:
    def __init__(self) -> None:
        self.published: list[str] = []

    def handler_available(self, census_kind: str, unit_kind: str) -> bool:
        del census_kind, unit_kind
        return True

    def publish(self, *, task_name: str, task_id: str, queue: str, payload_json: str) -> None:
        del task_name, task_id, queue
        self.published.append(payload_json)


class _Repo:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.admission = StandaloneCrmCensusAdmission(
            "census", "allocated", "fingerprint", "authority", "source", "control", True
        )
        self.attempt = StandaloneCrmAttempt(
            "census",
            1,
            "task",
            "running",
            1,
            now + timedelta(minutes=1),
            now + timedelta(minutes=2),
        )
        self.window: object | None = None
        self.failed = False
        self.reserved = 0
        self.no_source = 0
        self.ambiguous = 0
        self.terminalized = 0
        self.paused = 0
        self.status_census: dict[str, object] = {}

    def admit(self, request: object, *, authority: object) -> StandaloneCrmCensusAdmission:
        del request, authority
        return self.admission

    def claim_attempt(
        self, admission: object, request: object, *, task_id: str
    ) -> StandaloneCrmAttempt:
        del admission, request, task_id
        return self.attempt

    def status(self, census_id: str) -> StandaloneCrmCensusStatus:
        assert census_id == "census"
        return StandaloneCrmCensusStatus(self.status_census, (), (), (), ())

    def freeze_source_window(self, admission: object, attempt: object, window: object) -> int:
        del admission, attempt
        self.window = window
        return len(window.upper_bounds)

    def freeze_no_source_window(
        self, admission: object, attempt: object, *, unit_kind: str, revision_id: str
    ) -> None:
        del admission, attempt
        assert unit_kind in {"mapping_prepare", "mapping_rollback"}
        assert revision_id == "revision"
        self.no_source += 1

    def pause(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.paused += 1

    def freeze_failed(self, admission: object, attempt: object, *, reason: str) -> None:
        del admission, attempt
        assert reason == "freeze_incomplete"
        self.failed = True

    def reserve_publication(self, **kwargs: object) -> object:
        self.reserved += 1

        class P:
            publication_id = "publication"
            task_id = "child-task"
            payload_json = kwargs["payload_json"]
            task_name = "child"
            queue = "ingestion"

        return P()

    def mark_publication_publishing(self, *args: object) -> None:
        del args

    def mark_publication_ambiguous(self, *args: object) -> None:
        del args
        self.ambiguous += 1

    def confirm_publication(self, *args: object) -> None:
        del args

    def reconcile_terminal(
        self, admission: object, attempt: object
    ) -> tuple[str, StandaloneCrmTerminalAccounting]:
        del admission, attempt
        self.terminalized += 1
        return "completed", StandaloneCrmTerminalAccounting(1, 0, 0, 0, 1)


@pytest.mark.parametrize(
    "kinds",
    [
        ("contact",),
        ("lead",),
        ("company",),
        ("contact", "lead"),
        ("contact", "company"),
        ("lead", "company"),
        ("company", "contact", "lead"),
    ],
)
def test_every_nonempty_source_subset_freezes_selected_window(kinds: tuple[str, ...]) -> None:
    ordered = tuple(sorted(kinds))
    repo = _Repo()
    publisher = _Publisher()
    client = _Client({"contact": 0, "lead": 0, "company": 0})
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,
        admission=_Admission(),
        authority=_Authority(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: client,
    )  # type: ignore[arg-type]
    result = runtime.start_or_recover(_source(ordered), task_id="task")
    assert result.no_work_children == len(ordered)
    assert repo.window is not None
    assert repo.reserved == 0
    assert publisher.published == []


def test_partial_probe_failure_freezes_failed_without_publication() -> None:
    repo = _Repo()
    publisher = _Publisher()
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,
        admission=_Admission(),
        authority=_Authority(),
        publisher=publisher,
        probe_client_factory=lambda _adapter: _Client(
            {"contact": 1, "lead": 1, "company": 1}, True
        ),
    )  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="probe failed"):
        runtime.start_or_recover(_source(("contact",)), task_id="task")
    assert repo.failed is True
    assert publisher.published == []


@pytest.mark.parametrize(
    "census_request",
    [
        MappingPrepareCensusRequest(
            "bitrix_chat",
            "source",
            "control",
            "prepare",
            "operator",
            "policy",
            "association",
            "digest",
            _budget(),
            "revision",
            "revision-digest",
            None,
        ),
        MappingRollbackCensusRequest(
            "bitrix_chat",
            "source",
            "control",
            "rollback",
            "operator",
            "policy",
            "association",
            "digest",
            _budget(),
            "revision",
            "revision-digest",
            "head",
            None,
        ),
    ],
)
def test_mapping_requests_make_zero_source_client_calls(census_request: object) -> None:
    repo = _Repo()
    publisher = _Publisher()
    created = False

    def factory(_adapter: object) -> _Client:
        nonlocal created
        created = True
        return _Client({"contact": 1, "lead": 1, "company": 1})

    runtime = StandaloneCrmCensusRuntime(
        repository=cast("StandaloneCrmCensusRepository", repo),
        admission=_Admission(),
        authority=cast("StandaloneCrmAuthorityReader", _Authority()),
        publisher=publisher,
        probe_client_factory=factory,  # type: ignore[arg-type]
    )
    runtime.start_or_recover(census_request, task_id="task")  # type: ignore[arg-type]
    assert repo.no_source == 1
    assert created is False


class _ResumeRepo(_Repo):
    def __init__(self) -> None:
        super().__init__()
        self.request = _source(("contact",))
        self.authority = SourceSyncAuthoritySnapshot("mapping", "digest", None)
        self.continuation_task_ids: list[str] = []
        self.status_census = {
            "source_window_json": json.dumps(
                {
                    "selected_kinds": ["contact"],
                    "upper_bounds": [["contact", 1]],
                    "algorithm_version": "standalone-crm-source-window-v1",
                }
            )
        }

    def load_admitted_request(
        self, census_id: str
    ) -> tuple[StandaloneCrmCensusAdmission, SourceSyncCensusRequest, SourceSyncAuthoritySnapshot]:
        assert census_id == "census"
        return self.admission, self.request, self.authority

    def continue_attempt(
        self,
        admission: StandaloneCrmCensusAdmission,
        request: SourceSyncCensusRequest,
        *,
        task_id: str,
    ) -> StandaloneCrmAttempt:
        assert admission == self.admission
        assert request == self.request
        self.continuation_task_ids.append(task_id)
        return self.attempt


class _StaleSourceAuthority(_Authority):
    def __init__(self, stale_on_call: int) -> None:
        self._stale_on_call = stale_on_call
        self.calls = 0

    def source_sync_heads(self, request: SourceSyncCensusRequest) -> SourceSyncAuthoritySnapshot:
        del request
        self.calls += 1
        mapping_head = "changed" if self.calls >= self._stale_on_call else "mapping"
        return SourceSyncAuthoritySnapshot(mapping_head, "digest", None)


class _StaleMappingAuthority(_Authority):
    def __init__(self, stale_on_call: int) -> None:
        self._stale_on_call = stale_on_call
        self.calls = 0

    def validate_mapping_prepare(self, request: MappingPrepareCensusRequest) -> None:
        del request
        self.calls += 1
        if self.calls >= self._stale_on_call:
            raise RuntimeError("authority changed")


def test_stale_authority_before_source_publication_reservation_does_no_broker_io() -> None:
    repo = _Repo()
    publisher = _Publisher()
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=_StaleSourceAuthority(5),
        publisher=publisher,
        probe_client_factory=lambda _adapter: _Client({"contact": 1, "lead": 0, "company": 0}),
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.start_or_recover(_source(("contact",)), task_id="task")
    assert repo.reserved == 0
    assert repo.ambiguous == 0
    assert publisher.published == []


def test_stale_authority_after_source_reservation_marks_ambiguous_without_broker_io() -> None:
    repo = _Repo()
    publisher = _Publisher()
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=_StaleSourceAuthority(7),
        publisher=publisher,
        probe_client_factory=lambda _adapter: _Client({"contact": 1, "lead": 0, "company": 0}),
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.start_or_recover(_source(("contact",)), task_id="task")
    assert repo.reserved == 1
    assert repo.ambiguous == 1
    assert publisher.published == []


def test_stale_mapping_authority_before_reservation_does_no_broker_io() -> None:
    repo = _Repo()
    publisher = _Publisher()
    created = False

    def factory(_adapter: object) -> _Client:
        nonlocal created
        created = True
        return _Client({"contact": 1, "lead": 1, "company": 1})

    request = MappingPrepareCensusRequest(
        "bitrix_chat",
        "source",
        "control",
        "prepare",
        "operator",
        "policy",
        "association",
        "digest",
        _budget(),
        "revision",
        "revision-digest",
        None,
    )
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=_StaleMappingAuthority(3),
        publisher=publisher,
        probe_client_factory=factory,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.start_or_recover(request, task_id="task")
    assert created is False
    assert repo.reserved == 0
    assert repo.ambiguous == 0
    assert publisher.published == []


def test_stale_mapping_authority_after_reservation_marks_ambiguous() -> None:
    repo = _Repo()
    publisher = _Publisher()
    created = False

    def factory(_adapter: object) -> _Client:
        nonlocal created
        created = True
        return _Client({"contact": 1, "lead": 1, "company": 1})

    request = MappingPrepareCensusRequest(
        "bitrix_chat",
        "source",
        "control",
        "prepare",
        "operator",
        "policy",
        "association",
        "digest",
        _budget(),
        "revision",
        "revision-digest",
        None,
    )
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=_StaleMappingAuthority(5),
        publisher=publisher,
        probe_client_factory=factory,  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.start_or_recover(request, task_id="task")
    assert created is False
    assert repo.reserved == 1
    assert repo.ambiguous == 1
    assert publisher.published == []


def test_all_zero_source_requires_a_final_authority_revalidation_before_terminalization() -> None:
    repo = _Repo()
    publisher = _Publisher()
    runtime = StandaloneCrmCensusRuntime(
        repository=repo,  # type: ignore[arg-type]
        admission=_Admission(),
        authority=_StaleSourceAuthority(4),
        publisher=publisher,
        probe_client_factory=lambda _adapter: _Client({"contact": 0, "lead": 0, "company": 0}),
    )
    with pytest.raises(RuntimeError, match="authority changed"):
        runtime.start_or_recover(_source(("contact",)), task_id="task")
    assert repo.terminalized == 0
    assert repo.reserved == 0
    assert publisher.published == []


def test_source_handler_preflight_blocks_every_publication_when_any_kind_is_missing() -> None:
    repo = _Repo()
    publisher = _SelectivePublisher({"lead"})
    runtime = StandaloneCrmCensusRuntime(
        repository=cast(StandaloneCrmCensusRepository, repo),
        admission=_Admission(),
        authority=cast(StandaloneCrmAuthorityReader, _Authority()),
        publisher=publisher,
        probe_client_factory=lambda _adapter: _Client({"contact": 2, "lead": 3, "company": 0}),
    )
    result = runtime.start_or_recover(_source(("contact", "lead")), task_id="task")
    assert result.state == "paused_with_checkpoint"
    assert repo.reserved == 0
    assert repo.paused == 1
    assert publisher.published == []
    assert publisher.checked == ["contact", "lead"]


def test_resume_uses_the_generation_continuation_path_not_first_attempt_claim() -> None:
    repo = _ResumeRepo()
    publisher = _Publisher()
    runtime = StandaloneCrmCensusRuntime(
        repository=cast(StandaloneCrmCensusRepository, repo),
        admission=_Admission(),
        authority=cast(StandaloneCrmAuthorityReader, _Authority()),
        publisher=publisher,
        probe_client_factory=lambda _adapter: (_ for _ in ()).throw(AssertionError("no probe")),
    )
    result = runtime.resume("census", task_id="resume-delivery")
    assert repo.continuation_task_ids == ["resume-delivery"]
    assert repo.reserved == 1
    assert result.generation == 1
    assert len(publisher.published) == 1
