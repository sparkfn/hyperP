from __future__ import annotations

from contextlib import ExitStack
from typing import cast
from unittest.mock import MagicMock, patch

from neo4j import ManagedTransaction
from src.graph import queries
from src.models import MatchDecision, MatchResult, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.record_lifecycle import PlannedVersion


def _conversation() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="conversation-1",
        source_record_version="2",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-07-14T00:00:00Z",
        record_hash="changed",
        extraction_confidence=0.95,
        extraction_method="llm:test",
        raw_payload={"inquiries": []},
    )


def _run_conversation(
    decision: MatchDecision, *, knows_materializable: bool = True
) -> tuple[MagicMock, MagicMock, MagicMock]:
    pipeline = IngestPipeline(cast(object, MagicMock()))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(
            return_value=MatchResult(
                decision=decision,
                confidence=0.95,
                matched_person_id="person-1",
            )
        )
    )
    events: list[str] = []
    tx_mock = MagicMock()

    def record_query(query: str, **_params: object) -> MagicMock:
        if query == queries.RETIRE_KNOWS_PROJECTION:
            events.append("retire_knows")
        return MagicMock()

    tx_mock.run.side_effect = record_query
    tx = cast(ManagedTransaction, tx_mock)
    old_pk = "old-conversation"
    plan = PlannedVersion(2, old_pk, ("person-1",), None)
    with ExitStack() as stack:
        stack.enter_context(patch("src.pipeline.find_candidates", return_value=[]))
        stack.enter_context(patch("src.pipeline.upsert_nodes"))
        stack.enter_context(
            patch("src.pipeline.persist_source_record", return_value="new-conversation")
        )
        stack.enter_context(patch("src.pipeline.persist_match_decision", return_value="decision-1"))
        stack.enter_context(patch("src.pipeline.create_review_case_if_needed", return_value=None))
        stack.enter_context(patch("src.pipeline.link_record_to_graph"))
        stack.enter_context(patch("src.pipeline.retire_identity_projections", return_value=[]))
        stack.enter_context(patch("src.pipeline.activate_staged_version"))
        stack.enter_context(patch("src.pipeline.materialize_bankruptcy_case"))
        stack.enter_context(patch("src.pipeline.compute_golden_profile"))
        stack.enter_context(patch("src.pipeline.audit_person_pairs"))
        stack.enter_context(patch("src.pipeline.record_auto_merge_event"))
        knows = stack.enter_context(
            patch("src.pipeline.activate_knows_projection", return_value=knows_materializable)
        )
        knows.side_effect = lambda *_args: events.append("activate_knows") or knows_materializable
        writer = stack.enter_context(patch.object(pipeline, "_write_chat_vehicle_observations"))
        pipeline._execute_ingest(tx, _conversation(), [], [], [], lifecycle_plan=plan)
    cast_tx = cast(MagicMock, tx)
    cast_tx.projection_events = events
    return cast_tx, writer, knows


def test_accepted_conversation_retires_old_vehicle_mentions_before_writing_new() -> None:
    tx, writer, knows = _run_conversation(MatchDecision.MERGE)
    knows.assert_called_once()
    assert tx.projection_events.index("activate_knows") < tx.projection_events.index("retire_knows")
    tx.run.assert_any_call(
        queries.RETIRE_KNOWS_PROJECTION,
        source_record_pk="old-conversation",
    )
    tx.run.assert_any_call(
        queries.RETIRE_CONVERSATION_VEHICLE_MENTIONS,
        source_record_pk="old-conversation",
    )
    writer.assert_called_once()


def test_pending_conversation_does_not_retire_or_create_vehicle_mentions() -> None:
    tx, writer, knows = _run_conversation(MatchDecision.REVIEW)
    tx.run.assert_not_called()
    writer.assert_not_called()
    knows.assert_not_called()


def test_unresolved_replacement_relationship_retires_old_active_knows() -> None:
    tx, _writer, knows = _run_conversation(MatchDecision.MERGE, knows_materializable=False)
    knows.assert_called_once()
    tx.run.assert_any_call(
        queries.RETIRE_KNOWS_PROJECTION,
        source_record_pk="old-conversation",
    )
    tx.run.assert_any_call(
        queries.RETIRE_CONVERSATION_VEHICLE_MENTIONS,
        source_record_pk="old-conversation",
    )


def test_conversation_vehicle_retirement_is_source_record_scoped() -> None:
    query = queries.RETIRE_CONVERSATION_VEHICLE_MENTIONS
    assert "source_record_pk: $source_record_pk" in query
    assert "rel.is_active = false" in query


def test_pending_relationship_blueprint_is_available_for_review_activation() -> None:
    envelope = _conversation().model_copy(
        update={
            "raw_payload": {
                "primary_source_record_id": "primary-v1",
                "relationship_to_primary": "sister",
            }
        }
    )

    blueprint = IngestPipeline._activation_blueprint(envelope, MagicMock())

    assert blueprint["knows_relationships"] == [
        {
            "declarer_source_record_id": "primary-v1",
            "relationship_label": "sister",
            "relationship_category": "family",
            "status": "pending",
            "approved_at": None,
            "source_system_key": "bitrix_chat",
            "declarer_source_system_key": "bitrix_chat",
        }
    ]


def test_primary_chat_review_blueprint_contains_no_false_relationship() -> None:
    envelope = _conversation().model_copy(
        update={
            "raw_payload": {
                "primary_source_record_id": "chat-primary",
                "relationship_to_primary": None,
                "relationship_label": None,
            }
        }
    )

    blueprint = IngestPipeline._activation_blueprint(envelope, MagicMock())

    assert blueprint["knows_relationships"] == []
