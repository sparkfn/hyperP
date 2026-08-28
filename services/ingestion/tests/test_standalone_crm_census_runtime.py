"""Runtime admission, freezing, and no-live-call boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_models import (
    AuthorityHeads,
    CensusBudgets,
    CensusIdentity,
    CensusKind,
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    MissingCensusChildHandlerError,
    ParentState,
    SourceSyncCensusRequest,
)
from src.standalone_crm_census_runtime import (
    CensusChildPublisher,
    CensusProbeClient,
    MappingAuthorityReader,
    SourceAuthorityReader,
    SourceInstanceAdmitter,
    StandaloneCrmCensusRuntime,
)


class _FakeSourceInstances:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def admit(self, **kwargs: object) -> None:
        self.calls.append(("admit", kwargs))


class _FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def admit(self, **kwargs: object) -> tuple[str, bool]:
        self.calls.append(("admit", kwargs))
        return "census-1", True

    def claim_attempt(self, **kwargs: object) -> tuple[int, str]:
        self.calls.append(("claim_attempt", kwargs))
        return 1, "fence"

    def start_freezing(self, *, census_id: str, fingerprint: str) -> None:
        self.calls.append(("start_freezing", {"census_id": census_id, "fingerprint": fingerprint}))

    def reserve_http_call(self, **kwargs: object) -> bool:
        self.calls.append(("reserve_http_call", kwargs))
        return True

    def record_http_outcome(self, **kwargs: object) -> None:
        self.calls.append(("record_http_outcome", kwargs))

    def commit_source_window(self, **kwargs: object) -> None:
        self.calls.append(("commit_source_window", kwargs))

    def commit_no_source_window(self, *, census_id: str, fingerprint: str) -> None:
        self.calls.append(
            ("commit_no_source_window", {"census_id": census_id, "fingerprint": fingerprint})
        )

    def allocate_source_units(
        self, *, census_id: str, fingerprint: str, units: list[dict[str, object]]
    ) -> list[str]:
        self.calls.append(("allocate_source_units", {"units": units}))
        return [str(unit["unit_kind"]) for unit in units]

    def finalize(self, **kwargs: object) -> None:
        self.calls.append(("finalize", kwargs))

    def assert_ready(self) -> None:
        self.calls.append(("assert_ready", {}))

    def reserve_publication(self, **kwargs: object) -> str:
        self.calls.append(("reserve_publication", kwargs))
        return str(kwargs["task_id"])

    def pause(self, **kwargs: object) -> None:
        self.calls.append(("pause", kwargs))

    def confirm_publication(self, **kwargs: object) -> None:
        self.calls.append(("confirm_publication", kwargs))


def _identity() -> CensusIdentity:
    return CensusIdentity(
        source_instance_id="bitrix-primary",
        control_instance_id="legacy-default",
        occurrence_key="occurrence",
    )


def _budget() -> CensusBudgets:
    return CensusBudgets(
        attempt_calls=10,
        attempt_rows=100,
        attempt_runtime_seconds=60.0,
        occurrence_calls=20,
        occurrence_rows=1000,
        occurrence_wall_clock_seconds=600.0,
        max_attempts=3,
    )


class _SourceAuthority:
    def __init__(self, bounds: dict[str, int] | None = None) -> None:
        self.bounds = bounds or {}
        self.calls: list[tuple[str, object]] = []

    def source_heads(self) -> AuthorityHeads:
        self.calls.append(("source_heads", None))
        return AuthorityHeads(mapping_head="mapping-1", projection_head="projection-1")

    def probe_upper_id(self, kind: str) -> int:
        self.calls.append(("probe", kind))
        return self.bounds[kind]


class _MappingAuthority:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def prepare_head(self, request: MappingPrepareCensusRequest) -> AuthorityHeads:
        self.calls.append(("prepare_head", request))
        return AuthorityHeads(
            prepared_revision_id=request.prepared_revision_id,
            prepared_revision_digest=request.prepared_revision_digest,
        )

    def rollback_head(self, request: MappingRollbackCensusRequest) -> AuthorityHeads:
        self.calls.append(("rollback_head", request))
        return AuthorityHeads(
            prepared_revision_id=request.target_revision_id,
            prepared_revision_digest=request.target_revision_digest,
            rollback_head=request.rollback_head,
        )


def _runtime(
    *,
    authority: MappingAuthorityReader | SourceAuthorityReader | CensusProbeClient,
    repository: _FakeRepository,
    source_instances: object | None = None,
    publisher: object | None = None,
) -> StandaloneCrmCensusRuntime:
    class _Admitter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def admit(self, **kwargs: object) -> None:
            self.calls.append(("admit", kwargs))

    return StandaloneCrmCensusRuntime(
        source_repository=cast(SourceInstanceAdmitter, _Admitter()),
        census_repository=cast(StandaloneCrmCensusRepository, repository),
        authority=authority,
        clock=lambda: datetime.now(UTC),
        publisher=cast(CensusChildPublisher | None, publisher),
    )


def test_source_sync_freezes_all_selected_bounds_in_order() -> None:
    repository = _FakeRepository()
    authority = _SourceAuthority({"contact": 10, "lead": 0, "company": 3})
    runtime = _runtime(authority=authority, repository=repository)

    census_id = runtime.start(
        kind=CensusKind.SOURCE_SYNC,
        identity=_identity(),
        request=SourceSyncCensusRequest(
            selected_kinds=("contact", "lead", "company"), configuration_digest="config"
        ),
        budget=_budget(),
    )

    assert census_id == "census-1"
    reserve_calls = [call for name, call in repository.calls if name == "reserve_http_call"]
    assert [call["unit_kind"] for call in reserve_calls] == ["company", "contact", "lead"]
    probe_calls = [call for name, call in authority.calls if name == "probe"]
    assert probe_calls == ["company", "contact", "lead"]
    allocation = next(call for name, call in repository.calls if name == "allocate_source_units")
    assert [(unit["unit_kind"], unit["state"]) for unit in allocation["units"]] == [
        ("company", "pending_publication"),
        ("contact", "pending_publication"),
        ("lead", "completed"),
    ]


def test_probe_failure_freezes_failed_and_publishes_no_children() -> None:
    class _FailingAuthority(_SourceAuthority):
        def probe_upper_id(self, kind: str) -> int:
            raise RuntimeError("probe failed")

    repository = _FakeRepository()
    runtime = _runtime(authority=_FailingAuthority(), repository=repository)
    runtime.start(
        kind=CensusKind.SOURCE_SYNC,
        identity=_identity(),
        request=SourceSyncCensusRequest(selected_kinds=("contact",), configuration_digest="config"),
        budget=_budget(),
    )

    finalize_calls = [call for name, call in repository.calls if name == "finalize"]
    assert len(finalize_calls) == 1
    assert finalize_calls[0]["terminal_state"] == ParentState.FREEZE_FAILED.value

    assert any(name == "finalize" for name, _ in repository.calls)
    assert not any(name == "allocate_source_units" for name, _ in repository.calls)


def test_mapping_prepare_commits_no_source_window_and_makes_no_calls() -> None:
    repository = _FakeRepository()
    authority = _MappingAuthority()
    runtime = _runtime(authority=authority, repository=repository)

    census_id = runtime.start(
        kind=CensusKind.MAPPING_PREPARE,
        identity=_identity(),
        request=MappingPrepareCensusRequest(
            prepared_revision_id="revision",
            prepared_revision_digest="digest",
            expected_current_head="head",
        ),
        budget=_budget(),
    )

    assert census_id == "census-1"
    assert not any(name == "reserve_http_call" for name, _ in repository.calls)
    assert any(name == "commit_no_source_window" for name, _ in repository.calls)


def test_publish_child_without_handler_does_not_touch_broker() -> None:
    repository = _FakeRepository()
    runtime = _runtime(
        authority=_MappingAuthority(),
        repository=repository,
        publisher=None,
    )

    with pytest.raises(MissingCensusChildHandlerError):
        runtime.publish_child(
            census_id="census-1",
            fingerprint="fingerprint",
            fence_token="fence",
            unit_kind="contact",
            publication_sequence=1,
            payload_version="v1",
            payload={"unit_kind": "contact"},
        )
