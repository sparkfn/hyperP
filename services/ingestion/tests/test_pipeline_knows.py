"""Regression tests for post-ingest KNOWS materialization."""

from __future__ import annotations

from typing import Any

import pytest
from src import pipeline_knows
from src.graph import queries
from src.models import RecordType, SourceRecordEnvelope
from src.pipeline_knows import (
    _link_one_chat_relationship,
    _link_one_contact,
    materialize_knows_batch,
)


class _Row:
    def __init__(self, **values: object) -> None:
        self._values = values

    def __getitem__(self, key: str) -> object:
        return self._values[key]


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def single(self) -> _Row | None:
        return self._row

    def __iter__(self) -> object:
        return iter([self._row] if self._row is not None else [])


class _Tx:
    def __init__(self, declarer_source_system_key: str = "bitrix_chat") -> None:
        self.link_params: dict[str, object] | None = None
        self.declarer_source_system_key = declarer_source_system_key

    def run(self, query: str, **params: Any) -> _Result:
        if query == queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID:
            assert params == {
                "source_record_id": "bitrix-chat-1-person-1",
                "source_system_key": self.declarer_source_system_key,
            }
            return _Result(_Row(person_id="person-alice"))
        if query == queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_PK:
            assert params == {"source_record_pk": "pk-bob"}
            return _Result(_Row(person_id="person-bob"))
        if query == queries.LINK_PERSON_KNOWS:
            self.link_params = params
            return _Result(_Row(knows_id="knows-1"))
        if query == queries.MARK_PROFILE_ANALYSIS_DIRTY:
            return _Result(None)
        raise AssertionError(f"unexpected query: {query}")


def test_materialization_batch_rejects_unknown_phase() -> None:
    with pytest.raises(ValueError, match="Unknown KNOWS materialization phase"):
        materialize_knows_batch(object(), "unknown")  # type: ignore[arg-type]


def test_chat_relationship_materializer_creates_pending_knows() -> None:
    tx = _Tx()
    raw_payload = {
        "primary_source_record_id": "bitrix-chat-1-person-1",
        "relationship_to_primary": "brother",
        "relationship_label": "brother",
    }

    linked = _link_one_chat_relationship(tx, "pk-bob", "bitrix_chat", raw_payload)

    assert linked is True
    assert tx.link_params == {
        "declarer_person_id": "person-alice",
        "contact_person_id": "person-bob",
        "source_system_key": "bitrix_chat",
        "source_record_pk": "pk-bob",
        "relationship_label": "brother",
        "relationship_category": "family",
        "status": "pending",
        "approved_at": None,
    }


def test_relationship_scans_only_consider_active_source_versions() -> None:
    assert "sr.lifecycle_status = 'active'" in queries.SCAN_CONTACT_SOURCE_RECORDS
    assert "sr.lifecycle_status = 'active'" in queries.SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS
    assert "NOT EXISTS" in queries.SCAN_CONTACT_SOURCE_RECORDS
    assert "existing:KNOWS" in queries.SCAN_CONTACT_SOURCE_RECORDS
    assert "NOT EXISTS" in queries.SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS


def test_knows_resolution_and_projection_are_active_version_scoped() -> None:
    assert "sr.lifecycle_status = 'active'" in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "sr.lifecycle_status IS NULL" in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "sr.is_latest = true" in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "coalesce(sr.is_latest, true)" not in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "source_key: $source_system_key" in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "(sr)-[:FROM_SOURCE]->(:SourceSystem" in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "(sr)-[link:LINKED_TO]->(p:Person" in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    assert "coalesce(link.is_active, true) = true" in (queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID)
    assert (
        "SourceSystem {source_key: $source_system_key})\n      -[:LINKED_TO]"
        not in queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
    )
    assert "rel.is_active" in queries.LINK_PERSON_KNOWS
    assert "= true" in queries.LINK_PERSON_KNOWS
    assert "source_record_pk: $source_record_pk" in queries.RETIRE_KNOWS_PROJECTION
    assert "rel.is_active = false" in queries.RETIRE_KNOWS_PROJECTION


def test_link_knows_atomically_retires_prior_version_projection() -> None:
    query = queries.LINK_PERSON_KNOWS
    assert "PREVIOUS_VERSION_OF" in query
    assert "old_rel.is_active = false" in query
    assert "old_rel.retired_at = datetime()" in query


def test_activate_knows_projection_uses_selected_person_as_contact() -> None:
    tx = _Tx()
    envelope = SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="chat-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-07-14T00:00:00Z",
        record_hash="hash",
        extraction_confidence=0.9,
        extraction_method="llm:test",
        raw_payload={
            "primary_source_record_id": "bitrix-chat-1-person-1",
            "relationship_to_primary": "brother",
        },
    )

    activated = pipeline_knows.activate_knows_projection(tx, envelope, "person-bob", "pk-bob")

    assert activated is True
    assert tx.link_params is not None
    assert tx.link_params["contact_person_id"] == "person-bob"
    assert tx.link_params["source_record_pk"] == "pk-bob"


def test_primary_chat_record_without_relationship_does_not_activate_knows() -> None:
    tx = _Tx()
    envelope = SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="chat-primary",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-07-14T00:00:00Z",
        record_hash="hash-primary",
        extraction_confidence=0.9,
        extraction_method="llm:test",
        raw_payload={
            "primary_source_record_id": "chat-primary",
            "relationship_to_primary": None,
            "relationship_label": None,
        },
    )

    assert pipeline_knows.activate_knows_projection(tx, envelope, "reassigned-person", "primary-v2")
    assert tx.link_params is None
    assert pipeline_knows.knows_projection_blueprints(envelope) == []


def test_contact_projection_preserves_exact_source_system_provenance() -> None:
    tx = _Tx("fundbox")
    envelope = SourceRecordEnvelope(
        source_system="fundbox:contacts",
        source_record_id="contact-7",
        observed_at="2026-07-14T00:00:00Z",
        record_hash="contact-hash",
        raw_payload={
            "linked_to_source_record_id": "bitrix-chat-1-person-1",
            "link_type": "emergency contact",
        },
    )

    assert pipeline_knows.activate_knows_projection(tx, envelope, "person-bob", "contact-v2")
    assert tx.link_params is not None
    assert tx.link_params["source_system_key"] == "fundbox:contacts"
    blueprints = pipeline_knows.knows_projection_blueprints(envelope)
    assert blueprints[0]["source_system_key"] == "fundbox:contacts"
    assert blueprints[0]["declarer_source_system_key"] == "fundbox"


def test_contact_sweep_propagates_scanned_exact_source_system() -> None:
    tx = _Tx("fundbox")
    raw_payload = {
        "linked_to_source_record_id": "bitrix-chat-1-person-1",
        "link_type": "emergency contact",
    }

    linked = _link_one_contact(
        tx,
        "pk-bob",
        "fundbox:contacts",
        raw_payload,
    )

    assert linked is True
    assert tx.link_params is not None
    assert tx.link_params["source_system_key"] == "fundbox:contacts"
    assert "ss.source_key" in queries.SCAN_CONTACT_SOURCE_RECORDS


def test_bounded_batch_failure_replays_real_link_queries_without_undoing_prior_commit() -> None:
    committed: set[str] = set()
    scan_cursors: list[str] = []
    fail_after_linking: str | None = None

    class _ReplayTx:
        def __init__(self) -> None:
            self.pending = set(committed)

        def run(self, query: str, **params: Any) -> object:
            if query == queries.SCAN_CONTACT_SOURCE_RECORDS:
                cursor = str(params["cursor"])
                scan_cursors.append(cursor)
                candidate = "pk-1" if cursor == "" else "pk-2"
                if candidate in committed:
                    return []
                return [
                    {
                        "source_record_pk": candidate,
                        "source_system_key": "fundbox:contacts",
                        "raw_payload": (
                            '{"linked_to_source_record_id":"source-1",'
                            '"contact":{"relationship":"friend"}}'
                        ),
                    }
                ]
            if query == queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID:
                return _Result(_Row(person_id="declarer"))
            if query == queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_PK:
                return _Result(_Row(person_id=f"contact-{params['source_record_pk']}"))
            if query == queries.LINK_PERSON_KNOWS:
                self.pending.add(str(params["source_record_pk"]))
                return _Result(_Row(knows_id="knows"))
            if query == queries.MARK_PROFILE_ANALYSIS_DIRTY:
                source_record_pks = params["source_record_pks"]
                assert isinstance(source_record_pks, list)
                if fail_after_linking is not None and fail_after_linking in source_record_pks:
                    raise RuntimeError("transaction aborted after KNOWS write")
                return []
            raise AssertionError(f"unexpected query: {query}")

    class _ReplaySession:
        def __enter__(self) -> _ReplaySession:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def execute_write(self, work: Any) -> object:
            tx = _ReplayTx()
            result = work(tx)
            committed.clear()
            committed.update(tx.pending)
            return result

    class _ReplayClient:
        def session(self) -> _ReplaySession:
            return _ReplaySession()

    client = _ReplayClient()

    first = materialize_knows_batch(client, "contacts", cursor="")  # type: ignore[arg-type]
    assert first["next_cursor"] == "pk-1"
    assert committed == {"pk-1"}

    fail_after_linking = "pk-2"
    with pytest.raises(RuntimeError, match="transaction aborted after KNOWS write"):
        materialize_knows_batch(client, "contacts", cursor="pk-1")  # type: ignore[arg-type]
    assert committed == {"pk-1"}

    fail_after_linking = None
    replay = materialize_knows_batch(  # type: ignore[arg-type]
        client,
        "contacts",
        cursor="pk-1",
    )
    assert replay["next_cursor"] == "pk-2"
    assert committed == {"pk-1", "pk-2"}
    assert scan_cursors == ["", "pk-1", "pk-1"]
