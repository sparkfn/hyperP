from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

from pytest import MonkeyPatch
from src.connectors.base import SourceConnector
from src.exclusion_config import ExclusionFile
from src.exclusions import ExclusionContext
from src.graph.client import Neo4jClient
from src.ingestion_config import IngestionConfig, LlmConfig
from src.main import _ingest_all_records
from src.models import IngestResult, JsonValue, MatchDecision, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline


class _Connector(SourceConnector):
    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield {
            "source_record_id": "fundbox_consumer_backend-contact-1",
            "observed_at": "2026-05-15T00:00:00+00:00",
            "record_hash": "hash-1",
            "identifiers": [{"type": "phone", "value": "+6588888888"}],
            "attributes": {"full_name": "Internal Contact"},
            "raw_payload": {},
        }
        yield {
            "source_record_id": "fundbox_consumer_backend-contact-2",
            "observed_at": "2026-05-15T00:00:00+00:00",
            "record_hash": "hash-2",
            "identifiers": [{"type": "phone", "value": "+6599999999"}],
            "attributes": {"full_name": "Customer Contact"},
            "raw_payload": {},
        }

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend:contacts"


class _EmailConnector(SourceConnector):
    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield {
            "source_record_id": "fundbox_consumer_backend-user-1",
            "observed_at": "2026-05-15T00:00:00+00:00",
            "record_hash": "hash-1",
            "identifiers": [{"type": "email", "value": "info@ekolife.asia"}],
            "attributes": {"full_name": "Internal Email"},
            "raw_payload": {},
        }
        yield {
            "source_record_id": "fundbox_consumer_backend-user-2",
            "observed_at": "2026-05-15T00:00:00+00:00",
            "record_hash": "hash-2",
            "identifiers": [{"type": "email", "value": "customer@example.com"}],
            "attributes": {"full_name": "Customer Email"},
            "raw_payload": {},
        }

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend"


class _SalesConnector(SourceConnector):
    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield {
            "source_record_id": "speedzone-sale-1",
            "record_type": RecordType.SALES.value,
            "observed_at": "2026-05-15T00:00:00+00:00",
            "record_hash": "hash-sale-1",
            "identifiers": [],
            "attributes": {},
            "raw_payload": {"order": {"source_order_id": "order-1"}, "line_items": []},
        }

    def get_source_key(self) -> str:
        return "speedzone_phppos:sales"


@dataclass(frozen=True)
class _Settings:
    company_mobile_numbers: list[str] = field(default_factory=lambda: ["+6588888888"])
    company_email_addresses: list[str] = field(default_factory=list)
    internal_person_names: list[str] = field(default_factory=list)
    ingestion_exclusions_file: str = ""


@dataclass(frozen=True)
class _DomainSettings:
    company_mobile_numbers: list[str] = field(default_factory=list)
    company_email_addresses: list[str] = field(default_factory=list)
    internal_person_names: list[str] = field(default_factory=list)
    ingestion_exclusions_file: str = ""


class _Pipeline:
    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.exclusion_contexts: list[ExclusionContext | None] = []

    def ingest(
        self,
        envelope: SourceRecordEnvelope,
        ingest_run_id: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        self.ingested.append(envelope.source_record_id)
        self.exclusion_contexts.append(exclusion_context)
        return IngestResult(
            source_record_id=envelope.source_record_id,
            person_id="person-1",
            is_new_person=True,
            candidate_count=0,
            match_decision=MatchDecision.NO_MATCH,
            ingest_run_id=ingest_run_id,
        )


def test_ingest_all_records_uses_caller_supplied_exclusion_context(
    monkeypatch: MonkeyPatch,
) -> None:
    def _fail_get_ingestion_config() -> IngestionConfig:
        raise AssertionError("unexpected ingestion config load")

    monkeypatch.setattr("src.main.get_ingestion_config", _fail_get_ingestion_config)
    supplied_context = ExclusionContext(phones=frozenset({"+6588888888"}))
    pipeline = _Pipeline()

    success, errors, skipped = _ingest_all_records(
        client=cast(Neo4jClient, object()),
        pipeline=cast(IngestPipeline, pipeline),
        connector=_Connector(),
        ingest_run_id="run-1",
        exclusion_context=supplied_context,
    )

    assert (success, errors, skipped) == (1, 0, 1)
    assert pipeline.ingested == ["fundbox_consumer_backend-contact-2"]
    assert pipeline.exclusion_contexts == [supplied_context]


def test_ingest_all_records_skips_system_records_with_excluded_identifiers(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_settings", lambda: _Settings())
    monkeypatch.setattr("src.main.get_ingestion_config", lambda: IngestionConfig())
    pipeline = _Pipeline()

    success, errors, skipped = _ingest_all_records(
        client=cast(Neo4jClient, object()),
        pipeline=cast(IngestPipeline, pipeline),
        connector=_Connector(),
        ingest_run_id="run-1",
    )
    assert (success, errors, skipped) == (1, 0, 1)
    assert pipeline.ingested == ["fundbox_consumer_backend-contact-2"]
    assert len(pipeline.exclusion_contexts) == 1
    context = pipeline.exclusion_contexts[0]
    assert context is not None
    assert context.phones == frozenset({"+6588888888"})


def test_ingest_all_records_skips_system_records_with_excluded_email_domains(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_settings", lambda: _DomainSettings())
    monkeypatch.setattr(
        "src.main.get_ingestion_config",
        lambda: IngestionConfig(
            exclusions=ExclusionFile(email_domains=["ekolife.asia"]), llm=LlmConfig()
        ),
    )
    pipeline = _Pipeline()

    success, errors, skipped = _ingest_all_records(
        client=cast(Neo4jClient, object()),
        pipeline=cast(IngestPipeline, pipeline),
        connector=_EmailConnector(),
        ingest_run_id="run-1",
    )

    assert (success, errors, skipped) == (1, 0, 1)
    assert pipeline.ingested == ["fundbox_consumer_backend-user-2"]
    assert len(pipeline.exclusion_contexts) == 1
    context = pipeline.exclusion_contexts[0]
    assert context is not None
    assert context.email_domains == frozenset({"ekolife.asia"})


def test_ingest_all_records_passes_exclusion_context_to_sales_pipeline(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_settings", lambda: _DomainSettings())
    monkeypatch.setattr("src.main.get_ingestion_config", lambda: IngestionConfig())
    captured_contexts: list[ExclusionContext | None] = []

    def _fake_ingest_sales_record(
        client: Neo4jClient,
        envelope: SourceRecordEnvelope,
        *,
        ingest_run_id: str,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        captured_contexts.append(exclusion_context)
        return IngestResult(
            source_record_id=envelope.source_record_id,
            person_id="person-1",
            is_new_person=False,
            candidate_count=0,
            match_decision=None,
            ingest_run_id=ingest_run_id,
        )

    monkeypatch.setattr("src.main.ingest_sales_record", _fake_ingest_sales_record)

    success, errors, skipped = _ingest_all_records(
        client=cast(Neo4jClient, object()),
        pipeline=cast(IngestPipeline, _Pipeline()),
        connector=_SalesConnector(),
        ingest_run_id="run-1",
    )

    assert (success, errors, skipped) == (1, 0, 0)
    assert len(captured_contexts) == 1
    context = captured_contexts[0]
    assert context is not None
    assert context.phones == frozenset()
