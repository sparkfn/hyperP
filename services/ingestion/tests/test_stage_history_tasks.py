"""Default-off and topology tests for stage-history task publication."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src import (
    stage_history_review_task_runtime,
    stage_history_task_runtime,
    stage_history_tasks,
)
from src.ingestion_config import StageHistoryIngestionConfig


def test_replay_task_rejects_default_off_before_artifact_or_graph_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_history_task_runtime,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=StageHistoryIngestionConfig()),
    )

    def forbidden() -> None:
        raise AssertionError("disabled task touched runtime dependencies")

    monkeypatch.setattr(stage_history_task_runtime, "get_settings", forbidden)

    with pytest.raises(PermissionError, match="disabled"):
        stage_history_tasks.replay_stage_history_artifact_task.run("artifact-1", "approval-1")


def test_stage_tasks_are_manual_only_and_not_scheduled() -> None:
    schedule = stage_history_tasks.celery_app.conf.beat_schedule

    assert all("stage_history" not in str(entry) for entry in schedule.values())
    assert stage_history_tasks.replay_stage_history_artifact_task.name == (
        "src.stage_history_tasks.replay_stage_history_artifact_task"
    )
    assert stage_history_tasks.record_stage_history_capture_failure_task.name == (
        "src.stage_history_tasks.record_stage_history_capture_failure_task"
    )


def test_replay_authorization_is_bound_to_current_accepted_configuration() -> None:
    from src.connectors.bitrix_stage_history.artifact_manifest import (
        ArtifactManifest,
        canonical_metadata_json,
    )
    from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenance
    from src.connectors.bitrix_stage_history.connector import (
        StageCaptureLimits,
        stage_capture_limits_digest,
    )

    manifest = ArtifactManifest(
        schema_version=1,
        artifact_id="artifact-1",
        artifact_kind="stage-ingestion",
        created_at="2026-08-14T00:00:00Z",
        retention_expires_at="2026-08-15T00:00:00Z",
        metadata_json=canonical_metadata_json({}),
        files=(),
        provenance=ArtifactProvenance(
            artifact_path="/restricted/artifact-1",
            primary_device=1,
            primary_inode=1,
            backup_device=2,
            backup_inode=2,
            owner_uid=1,
            group_gid=1,
            directory_mode=0o500,
            source_contract_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            repository_sha="a" * 40,
            image_digest=f"sha256:{'b' * 64}",
            configuration_digest=f"sha256:{'c' * 64}",
            restricted_boundaries_json='{"upper_history_id":"redacted"}',
            counts_json='{"rows":1}',
            total_bytes=0,
        ),
        backup_path="/restricted-backup/artifact-1",
        backup_verified=True,
        signing_key_id="key-1",
        manifest_hmac="f" * 64,
    )
    config = StageHistoryIngestionConfig(
        authorization_reference="approval-1",
        authorized_actor="reviewer-1",
        owner_artifact_id="owner-accepted",
        owner_manifest_hmac="1" * 64,
        stage_artifact_id="stage-accepted",
        stage_manifest_hmac="2" * 64,
        qualification_evidence_digest=f"sha256:{'3' * 64}",
        accepted_configuration_digest=f"sha256:{'4' * 64}",
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        entity_type_id=2,
        max_calls=2,
        max_rows=100,
        max_spool_bytes=1_000_000,
        max_runtime_seconds=120.0,
    )

    authorization = stage_history_task_runtime._replay_authorization(
        "artifact-1",
        "approval-1",
        manifest,
        config,
        repository_sha="d" * 40,
        image_digest=f"sha256:{'e' * 64}",
    )

    assert authorization.source_contract_uuid == config.source_contract_uuid
    assert authorization.actor == config.authorized_actor
    assert authorization.repository_sha == "d" * 40
    assert authorization.image_digest == f"sha256:{'e' * 64}"
    assert authorization.owner_artifact_id == config.owner_artifact_id
    assert authorization.owner_manifest_hmac == config.owner_manifest_hmac
    assert authorization.stage_artifact_id == config.stage_artifact_id
    assert authorization.stage_manifest_hmac == config.stage_manifest_hmac
    assert authorization.configuration_digest == config.accepted_configuration_digest
    assert authorization.limits_digest == stage_capture_limits_digest(
        StageCaptureLimits(
            max_calls=2,
            max_rows=100,
            max_spool_bytes=1_000_000,
            max_runtime_seconds=120.0,
        )
    )


def test_replay_configuration_fingerprint_binds_retry_attempt_limit() -> None:
    from typing import cast

    from src.connectors.bitrix_stage_history.artifact_connector import (
        VerifiedStageIngestionArtifact,
    )

    artifact = cast(
        VerifiedStageIngestionArtifact,
        SimpleNamespace(
            manifest=SimpleNamespace(
                artifact_id="artifact-1",
                manifest_hmac="a" * 64,
                artifact_kind="stage-ingestion",
            )
        ),
    )

    baseline = stage_history_task_runtime._configuration_fingerprint(
        artifact,
        "approval-1",
        retry_max_attempts=5,
    )

    assert baseline != stage_history_task_runtime._configuration_fingerprint(
        artifact,
        "approval-1",
        retry_max_attempts=6,
    )


def test_review_domain_commit_is_replayed_when_logical_finalization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from src.bitrix_ingestion_models import FenceContext
    from src.config import Settings
    from src.graph.stage_history_review import (
        StageHistoryReviewExecution,
        StageHistoryReviewResult,
    )
    from src.stage_history_ingestion_models import (
        StageHistoryReviewCommand,
        stage_history_review_configuration_fingerprint,
    )

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
        event_identity="event-1",
        reviewer_id="reviewer-1",
        available_at=datetime(2026, 8, 14, tzinfo=UTC),
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state="withheld_conflict",
        expected_variant_set_digest=f"sha256:{'a' * 64}",
        selected_variant_hash=f"sha256:{'b' * 64}",
        selected_association_decision_id="association-1",
    )
    execution = StageHistoryReviewExecution(
        command=command,
        occurrence_id="occurrence-1",
        authorization_reference="approval-1",
        configuration_fingerprint=stage_history_review_configuration_fingerprint(
            "command-1",
            "resolve_conflict",
            "approval-1",
            review_lease_seconds=60,
            retry_backoff_seconds=300,
        ),
        worker_task_id="task-1",
        fence=fence,
    )

    class Client:
        def close(self) -> None:
            pass

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def load_execution(self, _command_id: str) -> StageHistoryReviewExecution:
            return execution

        def execute_command(self, *_args: object, **_kwargs: object) -> StageHistoryReviewResult:
            return StageHistoryReviewResult(
                command_id="command-1",
                authority_decision_id="decision-1",
                authority_state="effective",
                head_version=2,
                authority_token=2,
                invalidation_count=1,
            )

    failed: list[str] = []

    class Logical:
        def __init__(self, _client: object) -> None:
            pass

        def finalize_fenced(self, **_kwargs: object) -> None:
            raise RuntimeError("finalization unavailable")

        def fail_fenced(self, **_kwargs: object) -> None:
            failed.append("failed")

    class Lock:
        def assert_owned(self) -> None:
            pass

    monkeypatch.setattr(
        stage_history_review_task_runtime, "Neo4jClient", lambda _settings: Client()
    )
    monkeypatch.setattr(
        stage_history_review_task_runtime, "StageHistoryReviewRepository", Repository
    )
    monkeypatch.setattr(stage_history_review_task_runtime, "LogicalRunControl", Logical)

    with pytest.raises(RuntimeError, match="finalization unavailable"):
        stage_history_review_task_runtime._run_review_task(
            settings=Settings(),
            lock=Lock(),  # type: ignore[arg-type]
            task_id="task-1",
            command_id="command-1",
            authorization_reference="approval-1",
            config=StageHistoryIngestionConfig(
                authorization_reference="approval-1",
                authorized_actor="reviewer-1",
                review_lease_seconds=60,
            ),
        )

    assert failed == []


def test_review_task_rejects_default_off_before_graph_or_lock_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_history_review_task_runtime,
        "get_ingestion_config",
        lambda: SimpleNamespace(stage_history_ingestion=StageHistoryIngestionConfig()),
    )

    def forbidden() -> None:
        raise AssertionError("disabled review touched runtime dependencies")

    monkeypatch.setattr(stage_history_review_task_runtime, "get_settings", forbidden)

    with pytest.raises(PermissionError, match="disabled"):
        stage_history_tasks.execute_stage_history_review_task.run("command-1", "approval-1")


def test_review_worker_revalidates_the_authorized_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from src.bitrix_ingestion_models import FenceContext
    from src.config import Settings
    from src.graph.stage_history_review import StageHistoryReviewExecution
    from src.stage_history_ingestion_models import (
        StageHistoryReviewCommand,
        stage_history_review_configuration_fingerprint,
    )

    execution = StageHistoryReviewExecution(
        command=StageHistoryReviewCommand(
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
        ),
        occurrence_id="occurrence-1",
        authorization_reference="approval-1",
        configuration_fingerprint=stage_history_review_configuration_fingerprint(
            "command-1",
            "resolve_conflict",
            "approval-1",
            review_lease_seconds=900,
            retry_backoff_seconds=300,
        ),
        worker_task_id="task-1",
        fence=FenceContext(
            logical_run_id="logical-1",
            ingest_run_id="attempt-1",
            source_key="bitrix_chat",
            stream_key="crm_stage_history",
            stream_generation=1,
            fencing_token=1,
            attempt_generation=1,
        ),
    )

    class Client:
        def close(self) -> None:
            pass

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def load_execution(self, _command_id: str) -> StageHistoryReviewExecution:
            return execution

    class Lock:
        def assert_owned(self) -> None:
            pass

    monkeypatch.setattr(
        stage_history_review_task_runtime, "Neo4jClient", lambda _settings: Client()
    )
    monkeypatch.setattr(
        stage_history_review_task_runtime, "StageHistoryReviewRepository", Repository
    )

    with pytest.raises(PermissionError, match="identity changed"):
        stage_history_review_task_runtime._run_review_task(
            settings=Settings(),
            lock=Lock(),  # type: ignore[arg-type]
            task_id="task-1",
            command_id="command-1",
            authorization_reference="approval-1",
            config=StageHistoryIngestionConfig(
                authorization_reference="approval-1",
                authorized_actor="current-reviewer",
            ),
        )


def test_review_worker_rejects_changed_retry_configuration_before_domain_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from src.bitrix_ingestion_models import FenceContext
    from src.config import Settings
    from src.graph.stage_history_review import StageHistoryReviewExecution
    from src.stage_history_ingestion_models import (
        StageHistoryReviewCommand,
        stage_history_review_configuration_fingerprint,
    )

    execution = StageHistoryReviewExecution(
        command=StageHistoryReviewCommand(
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
        ),
        occurrence_id="occurrence-1",
        authorization_reference="approval-1",
        configuration_fingerprint=stage_history_review_configuration_fingerprint(
            "command-1",
            "resolve_parent",
            "approval-1",
            review_lease_seconds=900,
            retry_backoff_seconds=300,
        ),
        worker_task_id="task-1",
        fence=FenceContext(
            logical_run_id="logical-1",
            ingest_run_id="attempt-1",
            source_key="bitrix_chat",
            stream_key="crm_stage_history",
            stream_generation=1,
            fencing_token=1,
            attempt_generation=1,
        ),
    )

    class Client:
        def close(self) -> None:
            pass

    class Repository:
        def __init__(self, _client: object) -> None:
            pass

        def load_execution(self, _command_id: str) -> StageHistoryReviewExecution:
            return execution

        def execute_command(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("changed retry configuration mutated review state")

    class ForbiddenLogical:
        def __init__(self, _client: object) -> None:
            raise AssertionError("untrusted execution failed the logical run")

    class Lock:
        def assert_owned(self) -> None:
            pass

    monkeypatch.setattr(
        stage_history_review_task_runtime, "Neo4jClient", lambda _settings: Client()
    )
    monkeypatch.setattr(
        stage_history_review_task_runtime, "StageHistoryReviewRepository", Repository
    )
    monkeypatch.setattr(stage_history_review_task_runtime, "LogicalRunControl", ForbiddenLogical)

    with pytest.raises(PermissionError, match="identity changed"):
        stage_history_review_task_runtime._run_review_task(
            settings=Settings(),
            lock=Lock(),  # type: ignore[arg-type]
            task_id="task-1",
            command_id="command-1",
            authorization_reference="approval-1",
            config=StageHistoryIngestionConfig(
                authorization_reference="approval-1",
                authorized_actor="reviewer-1",
                review_lease_seconds=900,
                retry_backoff_seconds=301,
            ),
        )


def test_replay_admission_failure_releases_the_claimed_logical_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = SimpleNamespace(
        manifest=SimpleNamespace(
            artifact_kind="stage-ingestion",
            artifact_id="artifact-1",
            manifest_hmac="a" * 64,
        )
    )
    snapshot = SimpleNamespace(
        run_type="bounded_smoke_replay",
        connector_version="bitrix-crm-stagehistory-artifact-v1",
        schema_version=1,
    )
    attempt = SimpleNamespace(
        logical_run_id="logical-1",
        ingest_run_id="attempt-1",
        generation=1,
        worker_task_id="task-1",
        logical_status="queued",
    )
    failed: list[dict[str, object]] = []

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

        def fail_fenced(self, **_kwargs: object) -> None:
            raise AssertionError("admission failure cannot use an unissued stream fence")

    class Stream:
        def __init__(self, _client: object) -> None:
            pass

        def admit_or_coalesce(self, **_kwargs: object) -> object:
            raise RuntimeError("stream admission unavailable")

    class Lock:
        def assert_owned(self) -> None:
            pass

    monkeypatch.setattr(stage_history_task_runtime, "initial_replay_checkpoint", lambda _: snapshot)
    monkeypatch.setattr(stage_history_task_runtime, "_descriptor", lambda _: object())
    monkeypatch.setattr(stage_history_task_runtime, "LogicalRunControl", Logical)
    monkeypatch.setattr(stage_history_task_runtime, "BitrixStreamControl", Stream)

    with pytest.raises(RuntimeError, match="stream admission unavailable"):
        stage_history_task_runtime._run_source_free_replay(
            object(),  # type: ignore[arg-type]
            artifact=artifact,  # type: ignore[arg-type]
            worker_task_id="task-1",
            authorization_reference="approval-1",
            config=StageHistoryIngestionConfig(),
            failed_capture=False,
            lock=Lock(),  # type: ignore[arg-type]
        )

    assert failed == [
        {
            "logical_run_id": "logical-1",
            "ingest_run_id": "attempt-1",
            "generation": 1,
            "failure_category": "stage_history_execution_failed",
            "safe_failure_message": "RuntimeError",
        }
    ]
