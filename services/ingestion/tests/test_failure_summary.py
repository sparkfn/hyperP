from __future__ import annotations

import httpx
from pytest import MonkeyPatch
from src import main
from src.graph import queries
from src.graph.schema_init import BASE_LIFECYCLE_CONSTRAINTS


def test_failure_summary_classifies_timeout_and_retains_task_context() -> None:
    request = httpx.Request("POST", "https://whatsadmin.test/chats/query")
    error = httpx.ReadTimeout("upstream stalled", request=request)

    summary = main._build_failure_summary(
        error,
        source_key="whatsapp_chat",
        mode="api",
        task_id="task-123",
        checkpoint={"entity_key": "eko", "session_id": "ses-1", "cursor": "page-4"},
    )

    assert summary["category"] == "upstream_timeout"
    assert summary["exception_class"] == "ReadTimeout"
    assert summary["source"] == "whatsapp_chat"
    assert summary["mode"] == "api"
    assert summary["task_id"] == "task-123"
    assert summary["checkpoint"] == {
        "entity_key": "eko",
        "session_id": "ses-1",
        "cursor": "page-4",
    }


def test_failure_summary_classifies_known_sales_parameter_bug() -> None:
    error = RuntimeError("Expected parameter(s): entity_key")

    summary = main._build_failure_summary(
        error,
        source_key="eko_phppos:sales",
        mode="api",
        task_id=None,
        checkpoint={},
    )

    assert summary["category"] == "sales_entity_key"


def test_failure_summary_redacts_urls_and_secret_values() -> None:
    error = RuntimeError(
        "request to https://bitrix.test/rest/123/private-hook/crm.activity.list failed "
        "with api_key=private-key, {'token': 'private-token'} and "
        "Authorization: Bearer private-bearer"
    )

    summary = main._build_failure_summary(
        error,
        source_key="bitrix_chat",
        mode="backfill",
        task_id="task-123",
        checkpoint={},
    )

    assert "private-hook" not in summary["message"]
    assert "private-key" not in summary["message"]
    assert "private-token" not in summary["message"]
    assert "private-bearer" not in summary["message"]
    assert "[redacted-url]" in summary["message"]
    assert "api_key=[redacted]" in summary["message"]


def test_safe_resume_checkpoint_is_preserved_when_records_are_rejected() -> None:
    class Connector:
        committed = False
        discarded = False

        def commit_watermark(self) -> None:
            self.committed = True

        def discard_checkpoints(self) -> None:
            self.discarded = True

    connector = Connector()

    main._finalize_connector_progress(connector, error_count=1)

    assert connector.discarded is False
    assert connector.committed is False


def test_worker_created_ingest_run_retains_mode() -> None:
    assert "mode: $mode" in queries.CREATE_INGEST_RUN


def test_worker_created_ingest_run_reuses_the_stable_celery_task_identity() -> None:
    query = queries.CREATE_OR_REUSE_WORKER_INGEST_RUN

    assert (
        "MERGE (ir:IngestRun {control_instance_id: $control_instance_id, "
        "worker_task_id: $worker_task_id})" in query
    )
    assert "coalesce(ir.creation_token = $creation_token, false) AS created" in query
    assert "ir.source_key = $source_key AND ir.mode = $mode" in query
    assert not any(
        "ingest_run_worker_task_id_unique" in item for item in BASE_LIFECYCLE_CONSTRAINTS
    )


def test_terminal_worker_run_redelivery_skips_connector_creation(
    monkeypatch: MonkeyPatch,
) -> None:
    class GraphClient:
        closed = False

        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    client = GraphClient()
    monkeypatch.setattr(main, "get_settings", lambda: object())
    monkeypatch.setattr(main, "Neo4jClient", lambda _settings: client)
    pipeline_options: list[dict[str, object]] = []

    def _pipeline(_client: object, **kwargs: object) -> object:
        pipeline_options.append(kwargs)
        return object()

    monkeypatch.setattr(main, "IngestPipeline", _pipeline)
    monkeypatch.setattr(
        main,
        "_create_or_reuse_worker_ingest_run",
        lambda *_args, **_kwargs: ("run-1", "completed", False),
    )
    monkeypatch.setattr(
        main,
        "get_connector",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal redelivery must not create a connector")
        ),
    )

    result = main.run_ingestion(
        "fundbox",
        mode="api",
        initialize_graph=False,
        task_id="task-123",
    )

    assert result["ingest_run_id"] == "run-1"
    assert result["status"] == "completed"
    assert result["skipped"] == 1
    assert pipeline_options == [{"control_instance_id": "legacy-default"}]
    assert client.closed is True


def test_nonlegacy_direct_run_constructs_a_control_scoped_pipeline(
    monkeypatch: MonkeyPatch,
) -> None:
    class GraphClient:
        def verify_connectivity(self) -> None:
            return None

        def close(self) -> None:
            return None

    pipeline_options: list[dict[str, object]] = []

    def _pipeline(_client: object, **kwargs: object) -> object:
        pipeline_options.append(kwargs)
        return object()

    monkeypatch.setattr(main, "get_settings", lambda: object())
    monkeypatch.setattr(main, "Neo4jClient", lambda _settings: GraphClient())
    monkeypatch.setattr(main, "IngestPipeline", _pipeline)
    monkeypatch.setattr(
        main,
        "_create_or_reuse_worker_ingest_run",
        lambda *_args, **_kwargs: ("run-1", "completed", False),
    )

    result = main.run_ingestion(
        "fundbox",
        mode="api",
        initialize_graph=False,
        task_id="task-123",
        control_instance_id="portal-one",
    )

    assert result["status"] == "completed"
    assert pipeline_options == [{"control_instance_id": "portal-one"}]


def test_isolated_durable_failures_can_commit_connector_progress() -> None:
    class Connector:
        committed = False

        def commit_watermark(self) -> None:
            self.committed = True

        def commit_progress_with_errors(self) -> bool:
            return True

    connector = Connector()

    main._finalize_connector_progress(connector, error_count=1)

    assert connector.committed is True
