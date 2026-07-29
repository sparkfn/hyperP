from __future__ import annotations

import httpx
from src import main
from src.graph import queries


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
