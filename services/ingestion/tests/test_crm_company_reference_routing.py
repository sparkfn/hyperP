"""CRM-company records must bypass Person matching."""

from __future__ import annotations

from typing import cast

import pytest
from src import main
from src.exclusions import ExclusionContext
from src.models import IngestResult, MatchResult, SourceRecordEnvelope


def _company_raw_payload() -> dict[str, object]:
    return {
        "company_reference": {
            "type": "crm_company_id",
            "value": "303",
        },
        "reference_metadata": {
            "identity_policy_version": "crm_company_reference_v1",
            "source_instance_id": "bitrix-primary",
            "crm_company_id": "303",
            "person_matching_prohibited": True,
        },
    }


def test_company_record_routes_to_non_person_reference_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = SourceRecordEnvelope.model_validate(
        {
            "source_system": "bitrix_chat",
            "source_instance_id": "bitrix-primary",
            "source_record_id": "bitrix-crm-company-303",
            "record_type": "crm_company",
            "observed_at": None,
            "record_hash": "company-hash",
            "raw_payload": _company_raw_payload(),
        }
    )
    expected = IngestResult(source_record_id=envelope.source_record_id, source_record_pk="sr-303")
    calls: list[SourceRecordEnvelope] = []

    monkeypatch.setattr(
        main,
        "ingest_reference_record",
        lambda _client, received, ingest_run_id: (
            calls.append(received),
            expected,
        )[1],
    )
    monkeypatch.setattr(
        main,
        "ingest_address_record",
        lambda *_args, **_kwargs: pytest.fail("company record reached address ingestion"),
    )

    result = main._process_record(
        cast(object, object()),
        cast(object, object()),
        envelope,
        "run-1",
        ExclusionContext(),
    )

    assert result == expected
    assert calls == [envelope]


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        return None

    def execute_write(self, work: object) -> object:
        return cast(object, work)(object())  # type: ignore[operator]


class _Client:
    def session(self) -> _Session:
        return _Session()


def test_reference_persistence_rejects_non_company_records() -> None:
    from src import pipeline_references

    envelope = SourceRecordEnvelope.model_validate(
        {
            "source_system": "bitrix_chat",
            "source_instance_id": "bitrix-primary",
            "source_record_id": "bitrix-crm-contact-101",
            "record_type": "identity",
            "observed_at": None,
            "record_hash": "contact-hash",
        }
    )

    with pytest.raises(ValueError, match="only accepts crm_company"):
        pipeline_references.ingest_reference_record(cast(object, _Client()), envelope)


def test_company_reference_duplicate_skips_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import pipeline_references
    from src.record_lifecycle import DuplicateVersion

    envelope = SourceRecordEnvelope.model_validate(
        {
            "source_system": "bitrix_chat",
            "source_instance_id": "bitrix-primary",
            "source_record_id": "bitrix-crm-company-303",
            "record_type": "crm_company",
            "observed_at": None,
            "record_hash": "company-hash",
            "raw_payload": _company_raw_payload(),
        }
    )
    monkeypatch.setattr(
        pipeline_references,
        "load_locked_source_state",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        pipeline_references,
        "plan_incoming_version",
        lambda _state, _hash: DuplicateVersion("sr-existing"),
    )
    monkeypatch.setattr(
        pipeline_references,
        "persist_source_record",
        lambda *_args, **_kwargs: pytest.fail("duplicate reference was persisted"),
    )

    result = pipeline_references.ingest_reference_record(cast(object, _Client()), envelope)

    assert result.source_record_pk == "sr-existing"
    assert result.skipped_duplicate is True


def test_company_reference_persistence_never_creates_or_matches_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import pipeline_references
    from src.record_lifecycle import PlannedVersion

    envelope = SourceRecordEnvelope.model_validate(
        {
            "source_system": "bitrix_chat",
            "source_instance_id": "bitrix-primary",
            "source_record_id": "bitrix-crm-company-303",
            "record_type": "crm_company",
            "observed_at": None,
            "record_hash": "company-hash",
            "raw_payload": _company_raw_payload(),
        }
    )
    persisted: dict[str, object] = {}
    activation: dict[str, object] = {}

    monkeypatch.setattr(
        pipeline_references,
        "load_locked_source_state",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        pipeline_references,
        "plan_incoming_version",
        lambda _state, _hash: PlannedVersion(1, None, (), None),
    )
    monkeypatch.setattr(pipeline_references, "normalize_envelope_attributes", lambda _env: [])
    monkeypatch.setattr(
        pipeline_references,
        "persist_source_record",
        lambda _tx, **kwargs: (persisted.update(kwargs), "sr-303")[1],
    )
    monkeypatch.setattr(
        pipeline_references,
        "activate_staged_version",
        lambda _tx, **kwargs: activation.update(kwargs),
    )

    result = pipeline_references.ingest_reference_record(cast(object, _Client()), envelope)

    assert result.source_record_pk == "sr-303"
    assert persisted["identifiers"] == []
    assert persisted["is_new_person"] is False
    assert persisted["link_status"] == "not_applicable"
    match_result = persisted["match_result"]
    assert isinstance(match_result, MatchResult)
    assert match_result.decision.value == "no_match"
    assert activation["new_source_record_pk"] == "sr-303"
