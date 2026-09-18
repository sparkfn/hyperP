"""The pipeline's transaction-bound hook used by bounded writers."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Any

from src.models import IngestResult, SourceRecordEnvelope
from src.pipeline import IngestPipeline

_SENTINEL_TX = object()


class _Session:
    def __init__(self) -> None:
        self.transactions: list[object] = []

    def execute_write(self, work: Any) -> Any:
        self.transactions.append(_SENTINEL_TX)
        return work(_SENTINEL_TX)


class _Client:
    def __init__(self) -> None:
        self.session_obj = _Session()

    @contextmanager
    def session(self) -> Any:
        yield self.session_obj


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope.model_validate(
        {
            "source_system": "whatsapp_chat",
            "source_record_id": "whatsapp-chat-eko-ses_1-chat-1-person-1",
            "observed_at": "2026-09-17T05:20:00+00:00",
            "record_hash": "sha256:fixture",
            "record_type": "conversation",
            "extraction_confidence": 0.9,
            "extraction_method": "llm:fixture",
        }
    )


def test_ingest_delegates_to_the_transaction_hook_in_one_write_transaction() -> None:
    client = _Client()
    pipeline = IngestPipeline(client)  # type: ignore[arg-type]
    seen: dict[str, object] = {}

    def fake(
        tx: object,
        envelope: SourceRecordEnvelope,
        ingest_run_id: str | None = None,
        exclusion_context: object | None = None,
    ) -> IngestResult:
        seen["tx"] = tx
        seen["envelope"] = envelope
        seen["ingest_run_id"] = ingest_run_id
        seen["exclusion_context"] = exclusion_context
        return IngestResult(source_record_id=envelope.source_record_id)

    pipeline.ingest_in_transaction = fake  # type: ignore[method-assign]

    result = pipeline.ingest(_envelope(), ingest_run_id="run-1")

    assert result.source_record_id == "whatsapp-chat-eko-ses_1-chat-1-person-1"
    assert seen["tx"] is _SENTINEL_TX
    assert seen["ingest_run_id"] == "run-1"
    assert seen["exclusion_context"] is None
    assert client.session_obj.transactions == [_SENTINEL_TX]


def test_transaction_hook_signature_is_stable_for_bounded_writers() -> None:
    parameters = list(
        inspect.signature(IngestPipeline.ingest_in_transaction).parameters,
    )

    assert parameters == ["self", "tx", "envelope", "ingest_run_id", "exclusion_context"]


def test_transaction_hook_never_opens_its_own_session() -> None:
    """The bounded writer supplies the transaction; the hook must not open one."""
    source = inspect.getsource(IngestPipeline.ingest_in_transaction)

    assert "self._client" not in source
    assert "execute_write" not in source
    assert "session(" not in source
