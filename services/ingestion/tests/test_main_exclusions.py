from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import cast

from pytest import MonkeyPatch
from src.connectors.base import SourceConnector
from src.exclusion_config import ExclusionFile
from src.main import _ingest_all_records
from src.models import IngestResult, JsonValue, MatchDecision, SourceRecordEnvelope


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

    def ingest(
        self,
        envelope: SourceRecordEnvelope,
        ingest_run_id: str | None = None,
    ) -> IngestResult:
        self.ingested.append(envelope.source_record_id)
        return IngestResult(
            source_record_id=envelope.source_record_id,
            person_id="person-1",
            is_new_person=True,
            candidate_count=0,
            match_decision=MatchDecision.NO_MATCH,
            ingest_run_id=ingest_run_id,
        )


def test_ingest_all_records_skips_system_records_with_excluded_identifiers(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_settings", lambda: _Settings())
    pipeline = _Pipeline()

    success, errors, skipped = _ingest_all_records(
        client=cast("object", object()),
        pipeline=cast("object", pipeline),
        connector=_Connector(),
        ingest_run_id="run-1",
    )
    assert (success, errors, skipped) == (1, 0, 1)
    assert pipeline.ingested == ["fundbox_consumer_backend-contact-2"]


def test_ingest_all_records_skips_system_records_with_excluded_email_domains(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.get_settings", lambda: _DomainSettings())
    monkeypatch.setattr(
        "src.main.load_exclusion_file",
        lambda _path: ExclusionFile(email_domains=["ekolife.asia"]),
    )
    pipeline = _Pipeline()

    success, errors, skipped = _ingest_all_records(
        client=cast("object", object()),
        pipeline=cast("object", pipeline),
        connector=_EmailConnector(),
        ingest_run_id="run-1",
    )

    assert (success, errors, skipped) == (1, 0, 1)
    assert pipeline.ingested == ["fundbox_consumer_backend-user-2"]
