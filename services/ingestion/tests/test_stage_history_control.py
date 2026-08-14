"""Operator command gates for the bounded stage-history smoke path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src import stage_history_control, stage_history_review_control
from src.ingestion_config import StageHistoryIngestionConfig


def test_collect_smoke_is_rejected_before_source_or_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=StageHistoryIngestionConfig()),
    )

    def forbidden() -> None:
        raise AssertionError("disabled collection touched runtime dependencies")

    monkeypatch.setattr(stage_history_control, "get_settings", forbidden)

    with pytest.raises(PermissionError, match="disabled"):
        stage_history_control.run(["collect-smoke"])


def test_dispatch_smoke_is_rejected_before_artifact_or_task_access_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=StageHistoryIngestionConfig()),
    )

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled dispatch touched artifact or task runtime")

    monkeypatch.setattr(stage_history_control, "_verified_artifact", forbidden)
    monkeypatch.setattr(
        stage_history_control.replay_stage_history_artifact_task,
        "apply_async",
        forbidden,
    )

    with pytest.raises(PermissionError, match="disabled"):
        stage_history_control.run(
            [
                "dispatch-smoke",
                "--artifact-id",
                "artifact-1",
                "--authorization-reference",
                "approval-1",
            ]
        )


def test_dispatch_smoke_verifies_artifact_kind_before_task_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    config = StageHistoryIngestionConfig(
        enabled=True,
        authorization_reference="approval-1",
        authorization_expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=config),
    )
    monkeypatch.setattr(
        stage_history_control,
        "_verified_artifact",
        lambda *_args: SimpleNamespace(
            manifest=SimpleNamespace(artifact_kind="stage-ingestion-failed")
        ),
    )

    def forbidden_publish(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("mismatched artifact was published")

    monkeypatch.setattr(
        stage_history_control.replay_stage_history_artifact_task,
        "apply_async",
        forbidden_publish,
    )

    with pytest.raises(ValueError, match="artifact kind"):
        stage_history_control.run(
            [
                "dispatch-smoke",
                "--artifact-id",
                "artifact-1",
                "--authorization-reference",
                "approval-1",
            ]
        )


def test_parser_exposes_only_bounded_manual_commands() -> None:
    parser = stage_history_control.build_parser()

    assert parser.parse_args(["collect-smoke"]).command == "collect-smoke"
    assert (
        parser.parse_args(
            [
                "dispatch-smoke",
                "--artifact-id",
                "artifact-1",
                "--authorization-reference",
                "approval-1",
            ]
        ).command
        == "dispatch-smoke"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["full-backfill"])


def test_resume_parser_accepts_artifact_or_review_command_but_not_both() -> None:
    parser = stage_history_control.build_parser()

    assert (
        parser.parse_args(
            [
                "resume",
                "--artifact-id",
                "artifact-1",
                "--authorization-reference",
                "approval-1",
            ]
        ).artifact_id
        == "artifact-1"
    )
    assert (
        parser.parse_args(
            [
                "resume",
                "--command-id",
                "command-1",
                "--authorization-reference",
                "approval-1",
            ]
        ).command_id
        == "command-1"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "resume",
                "--artifact-id",
                "artifact-1",
                "--command-id",
                "command-1",
                "--authorization-reference",
                "approval-1",
            ]
        )


def test_correction_requires_explicit_current_authority_state() -> None:
    parser = stage_history_control.build_parser()
    arguments = parser.parse_args(
        [
            "apply-correction",
            "--event-identity",
            "event-1",
            "--occurrence-id",
            "occurrence-1",
            "--reviewer",
            "reviewer-1",
            "--authorization-reference",
            "approval-1",
            "--expected-head-version",
            "1",
            "--expected-authority-token",
            "1",
            "--expected-variant-set-digest",
            f"sha256:{'a' * 64}",
            "--selected-variant-hash",
            f"sha256:{'b' * 64}",
            "--selected-association-decision-id",
            "association-1",
            "--correction-of-decision-id",
            "authority-0",
        ]
    )
    config = StageHistoryIngestionConfig(
        enabled=True,
        authorization_reference="approval-1",
        authorized_actor="reviewer-1",
    )

    with pytest.raises(ValueError, match="current authority state"):
        stage_history_review_control._queue_review(arguments, config)


def test_review_publication_failure_fails_the_claimed_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from src.bitrix_ingestion_models import FenceContext
    from src.config import Settings
    from src.stage_history_ingestion_models import StageHistoryReviewCommand

    events: list[object] = []
    created: list[dict[str, object]] = []
    fence = FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="attempt-1",
        source_key="bitrix_chat",
        stream_key="crm_stage_history",
        stream_generation=1,
        fencing_token=1,
        attempt_generation=1,
    )
    command = StageHistoryReviewCommand(
        command_id="command-1",
        kind="resolve_conflict",
        status="pending",
        event_identity="event-sensitive",
        reviewer_id="reviewer-1",
        available_at=datetime(2026, 8, 14, tzinfo=UTC),
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state="withheld_conflict",
        expected_variant_set_digest=f"sha256:{'a' * 64}",
        selected_variant_hash=f"sha256:{'b' * 64}",
        selected_association_decision_id="association-sensitive",
    )

    class Client:
        def close(self) -> None:
            events.append("close")

    class Logical:
        def create_or_reuse(self, **kwargs: object) -> object:
            created.append(kwargs)
            events.append("create")
            return SimpleNamespace(
                worker_task_id="task-1",
                logical_run_id="logical-1",
                ingest_run_id="attempt-1",
                generation=1,
            )

        def claim(self, **_kwargs: object) -> bool:
            events.append("claim")
            return True

    logical = Logical()

    class Stream:
        def admit_or_coalesce(self, **_kwargs: object) -> object:
            events.append("admit")
            return SimpleNamespace(fence_context=fence)

    class Repository:
        def record_command(self, *_args: object, **_kwargs: object) -> None:
            events.append("record")

    class Lock:
        def assert_owned(self) -> None:
            events.append("lock")

    published: list[tuple[tuple[object, ...], str, str]] = []

    def publish(*, args: tuple[object, ...], task_id: str, queue: str) -> None:
        published.append((args, task_id, queue))
        events.append("publish")
        raise RuntimeError("broker unavailable")

    def fail_publication(
        _settings: Settings,
        *,
        fence: object,
        task_id: str,
        error: Exception,
    ) -> None:
        events.append(("fail", fence, task_id, type(error).__name__))

    monkeypatch.setattr(stage_history_review_control, "Neo4jClient", lambda _settings: Client())
    monkeypatch.setattr(stage_history_review_control, "LogicalRunControl", lambda _client: logical)
    monkeypatch.setattr(
        stage_history_review_control, "BitrixStreamControl", lambda _client: Stream()
    )
    monkeypatch.setattr(
        stage_history_review_control,
        "StageHistoryReviewRepository",
        lambda _client: Repository(),
    )
    monkeypatch.setattr(
        stage_history_review_control.execute_stage_history_review_task,
        "apply_async",
        publish,
    )
    monkeypatch.setattr(stage_history_review_control, "_fail_review_publication", fail_publication)

    prepared = stage_history_review_control._queue_review_locked(
        command,
        occurrence_id="occurrence-sensitive",
        authorization_reference="approval-1",
        run_type="conflict_review",
        configuration_fingerprint="c" * 64,
        checkpoint=stage_history_review_control._review_checkpoint(
            "command-1", f"sha256:{'c' * 64}"
        ),
        task_id="task-1",
        settings=Settings(),
        lock=Lock(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="broker unavailable"):
        stage_history_review_control._publish_review(prepared, settings=Settings())

    assert published == [(("command-1", "approval-1"), "task-1", "ingestion")]
    assert created[0]["configuration_fingerprint"] == "c" * 64
    assert events.index("record") < events.index("publish")
    assert ("fail", fence, "task-1", "RuntimeError") in events


def test_review_admission_failure_releases_the_claimed_logical_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from src.config import Settings
    from src.stage_history_ingestion_models import StageHistoryReviewCommand

    command = StageHistoryReviewCommand(
        command_id="command-1",
        kind="resolve_conflict",
        status="pending",
        event_identity="event-1",
        reviewer_id="reviewer-1",
        available_at=datetime(2026, 8, 14, tzinfo=UTC),
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state="withheld_conflict",
        expected_variant_set_digest=f"sha256:{'a' * 64}",
        selected_variant_hash=f"sha256:{'b' * 64}",
    )
    attempt = SimpleNamespace(
        worker_task_id="task-1",
        logical_run_id="logical-1",
        ingest_run_id="attempt-1",
        generation=1,
    )
    failed: list[dict[str, object]] = []

    class Client:
        def close(self) -> None:
            pass

    class Logical:
        def __init__(self, _client: object) -> None:
            pass

        def create_or_reuse(self, **_kwargs: object) -> object:
            return attempt

        def claim(self, **_kwargs: object) -> bool:
            return True

        def fail(self, **kwargs: object) -> bool:
            failed.append(kwargs)
            return True

    class Stream:
        def __init__(self, _client: object) -> None:
            pass

        def admit_or_coalesce(self, **_kwargs: object) -> object:
            raise RuntimeError("stream admission unavailable")

    class Lock:
        def assert_owned(self) -> None:
            pass

    monkeypatch.setattr(stage_history_review_control, "Neo4jClient", lambda _: Client())
    monkeypatch.setattr(stage_history_review_control, "LogicalRunControl", Logical)
    monkeypatch.setattr(stage_history_review_control, "BitrixStreamControl", Stream)

    with pytest.raises(RuntimeError, match="stream admission unavailable"):
        stage_history_review_control._queue_review_locked(
            command,
            occurrence_id="occurrence-1",
            authorization_reference="approval-1",
            run_type="conflict_review",
            configuration_fingerprint="c" * 64,
            checkpoint=stage_history_review_control._review_checkpoint(
                "command-1", f"sha256:{'c' * 64}"
            ),
            task_id="task-1",
            settings=Settings(),
            lock=Lock(),  # type: ignore[arg-type]
        )

    assert failed == [
        {
            "logical_run_id": "logical-1",
            "ingest_run_id": "attempt-1",
            "generation": 1,
            "failure_category": "stage_history_review_publication_failed",
            "safe_failure_message": "RuntimeError",
        }
    ]


def test_request_stop_is_rejected_before_graph_access_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=StageHistoryIngestionConfig()),
    )

    def forbidden() -> None:
        raise AssertionError("disabled stop request touched graph runtime")

    monkeypatch.setattr(stage_history_control, "get_settings", forbidden)

    with pytest.raises(PermissionError, match="disabled"):
        stage_history_control.run(
            [
                "request-stop",
                "--logical-run-id",
                "logical-1",
                "--actor",
                "operator-1",
                "--reason",
                "bounded stop",
            ]
        )


def test_reconcile_is_rejected_before_artifact_or_graph_access_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=StageHistoryIngestionConfig()),
    )

    def forbidden() -> None:
        raise AssertionError("disabled reconcile touched runtime dependencies")

    monkeypatch.setattr(stage_history_control, "get_settings", forbidden)

    with pytest.raises(PermissionError, match="disabled"):
        stage_history_control.run(
            [
                "reconcile",
                "--logical-run-id",
                "logical-1",
                "--artifact-id",
                "artifact-1",
                "--authorization-reference",
                "approval-1",
            ]
        )


def test_request_stop_cannot_mutate_a_non_stage_logical_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    config = StageHistoryIngestionConfig(
        enabled=True,
        authorization_reference="approval-1",
        authorized_actor="operator-1",
        authorization_expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=config),
    )
    monkeypatch.setattr(stage_history_control, "get_settings", lambda: object())

    class Client:
        def close(self) -> None:
            pass

    class Logical:
        def __init__(self, _client: object) -> None:
            pass

        def get(self, _logical_run_id: str) -> object:
            return SimpleNamespace(source_key="fundbox", mode="incremental")

        def request_stop(self, **_kwargs: object) -> object:
            raise AssertionError("non-stage run was mutated")

    monkeypatch.setattr(stage_history_control, "Neo4jClient", lambda _settings: Client())
    monkeypatch.setattr(stage_history_control, "LogicalRunControl", Logical)

    result = stage_history_control.run(
        [
            "request-stop",
            "--logical-run-id",
            "logical-1",
            "--actor",
            "operator-1",
            "--reason",
            "bounded stop",
        ]
    )

    assert result == 1


def test_review_actor_must_match_the_authorized_actor_before_graph_access() -> None:
    args = stage_history_control.build_parser().parse_args(
        [
            "resolve-conflict",
            "--event-identity",
            "event-1",
            "--occurrence-id",
            "occurrence-1",
            "--reviewer",
            "wrong-reviewer",
            "--authorization-reference",
            "approval-1",
            "--expected-head-version",
            "1",
            "--expected-authority-token",
            "1",
            "--expected-variant-set-digest",
            f"sha256:{'a' * 64}",
        ]
    )
    config = StageHistoryIngestionConfig(
        authorization_reference="approval-1",
        authorized_actor="authorized-reviewer",
    )

    with pytest.raises(PermissionError, match="actor changed"):
        stage_history_review_control._queue_review(args, config)


def test_review_resume_revalidates_the_authorized_actor_before_run_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from datetime import UTC, datetime

    from src.stage_history_ingestion_models import StageHistoryReviewCommand

    command = StageHistoryReviewCommand(
        command_id="command-1",
        kind="resolve_conflict",
        status="pending",
        event_identity="event-1",
        reviewer_id="prior-reviewer",
        available_at=datetime(2026, 8, 14, tzinfo=UTC),
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state="withheld_conflict",
        expected_variant_set_digest=f"sha256:{'a' * 64}",
        selected_variant_hash=f"sha256:{'b' * 64}",
    )
    context = SimpleNamespace(
        command=command,
        authorization_reference="approval-1",
        logical_status="failed",
        logical_run_id="logical-1",
    )

    class Client:
        def close(self) -> None:
            pass

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def load_resume_context(self, _command_id: str) -> object:
            return context

    class Lock:
        def assert_owned(self) -> None:
            pass

    @contextmanager
    def lock_context(*_args: object, **_kwargs: object) -> Iterator[Lock]:
        yield Lock()

    def forbidden_logical(_client: object) -> object:
        raise AssertionError("changed review actor mutated the logical run")

    monkeypatch.setattr(
        stage_history_review_control,
        "get_settings",
        lambda: SimpleNamespace(celery_broker_url="redis://unused"),
    )
    monkeypatch.setattr(stage_history_review_control, "stage_history_task_lock", lock_context)
    monkeypatch.setattr(stage_history_review_control, "Neo4jClient", lambda _: Client())
    monkeypatch.setattr(stage_history_review_control, "StageHistoryReviewRepository", Repository)
    monkeypatch.setattr(stage_history_review_control, "LogicalRunControl", forbidden_logical)

    with pytest.raises(PermissionError, match="actor changed"):
        stage_history_review_control._resume_review(
            "command-1",
            "approval-1",
            StageHistoryIngestionConfig(
                authorization_reference="approval-1",
                authorized_actor="current-reviewer",
            ),
        )


def test_review_resume_rejects_changed_retry_configuration_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from datetime import UTC, datetime

    from src.stage_history_ingestion_models import (
        StageHistoryReviewCommand,
        stage_history_review_configuration_fingerprint,
    )

    command = StageHistoryReviewCommand(
        command_id="command-1",
        kind="resolve_parent",
        status="pending",
        event_identity="event-1",
        reviewer_id="reviewer-1",
        available_at=datetime(2026, 8, 14, tzinfo=UTC),
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state="withheld_parent",
        expected_variant_set_digest=f"sha256:{'a' * 64}",
        retry_sequence=1,
    )
    context = SimpleNamespace(
        command=command,
        authorization_reference="approval-1",
        configuration_fingerprint=stage_history_review_configuration_fingerprint(
            "command-1",
            "resolve_parent",
            "approval-1",
            review_lease_seconds=900,
            retry_backoff_seconds=300,
        ),
        logical_status="queued",
        logical_run_id="logical-1",
    )

    class Client:
        def close(self) -> None:
            pass

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def load_resume_context(self, _command_id: str) -> object:
            return context

    class Lock:
        def assert_owned(self) -> None:
            pass

    @contextmanager
    def lock_context(*_args: object, **_kwargs: object) -> Iterator[Lock]:
        yield Lock()

    def forbidden_logical(_client: object) -> object:
        raise AssertionError("changed retry configuration mutated the logical run")

    monkeypatch.setattr(
        stage_history_review_control,
        "get_settings",
        lambda: SimpleNamespace(celery_broker_url="redis://unused"),
    )
    monkeypatch.setattr(stage_history_review_control, "stage_history_task_lock", lock_context)
    monkeypatch.setattr(stage_history_review_control, "Neo4jClient", lambda _: Client())
    monkeypatch.setattr(stage_history_review_control, "StageHistoryReviewRepository", Repository)
    monkeypatch.setattr(stage_history_review_control, "LogicalRunControl", forbidden_logical)

    with pytest.raises(PermissionError, match="configuration changed"):
        stage_history_review_control._resume_review(
            "command-1",
            "approval-1",
            StageHistoryIngestionConfig(
                authorization_reference="approval-1",
                authorized_actor="reviewer-1",
                review_lease_seconds=900,
                retry_backoff_seconds=301,
            ),
        )


def test_correction_requires_and_preserves_an_explicit_current_authority_state() -> None:
    parser = stage_history_control.build_parser()
    base = [
        "apply-correction",
        "--event-identity",
        "event-1",
        "--occurrence-id",
        "occurrence-1",
        "--reviewer",
        "authorized-reviewer",
        "--authorization-reference",
        "approval-1",
        "--expected-head-version",
        "4",
        "--expected-authority-token",
        "7",
        "--expected-variant-set-digest",
        f"sha256:{'a' * 64}",
        "--selected-variant-hash",
        f"sha256:{'b' * 64}",
        "--selected-association-decision-id",
        "association-1",
        "--correction-of-decision-id",
        "decision-3",
    ]
    config = StageHistoryIngestionConfig(
        authorization_reference="approval-1",
        authorized_actor="authorized-reviewer",
    )

    with pytest.raises(ValueError, match="current authority state"):
        stage_history_review_control._queue_review(parser.parse_args(base), config)


def test_failed_artifact_resume_uses_failure_accounting_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        authorization_reference="approval-1",
        assert_dispatch_enabled=lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        stage_history_control,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=config),
    )
    monkeypatch.setattr(
        stage_history_control,
        "_verified_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(
            manifest=SimpleNamespace(artifact_kind="stage-ingestion-failed")
        ),
    )
    published: list[tuple[tuple[str, str], str]] = []
    monkeypatch.setattr(
        stage_history_control.record_stage_history_capture_failure_task,
        "apply_async",
        lambda *, args, task_id, queue: published.append((args, queue)),
    )

    assert (
        stage_history_control.run(
            [
                "resume",
                "--artifact-id",
                "artifact-1",
                "--authorization-reference",
                "approval-1",
            ]
        )
        == 0
    )

    assert published == [(("artifact-1", "approval-1"), "ingestion")]
