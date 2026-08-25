"""Guardrails for Bitrix CRM-deal identity evidence and resolution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock, patch

from neo4j import ManagedTransaction
from src.connectors.bitrix_openlines.connector import _deal_envelope as build_deal_envelope
from src.connectors.bitrix_openlines.crm_identity_policy import (
    CRM_DEAL_IDENTITY_POLICY_VERSION,
    MAX_CRM_CONTACT_EMAILS,
    MAX_CRM_CONTACT_PHONES,
    crm_contact_identity_evidence,
)
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
    SourceRecordEnvelope,
)
from src.pipeline import IngestPipeline
from src.pipeline_crm_identity import (
    apply_crm_deal_match_policy,
    crm_deal_requires_quarantine,
    projected_identifiers,
    resolve_canonical_crm_contact,
)
from src.pipeline_normalization import normalize_envelope_identifiers


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Tx:
    def __init__(
        self,
        person_ids: list[str],
        *,
        blocked_owners: dict[str, str] | None = None,
    ) -> None:
        self._person_ids = person_ids
        self._blocked_owners = blocked_owners or {}

    def run(self, query: str, **_kwargs: object) -> _Rows:
        if "candidate_person_id" in query and "NO_MATCH_LOCK" in query:
            return _Rows(
                [
                    {
                        "candidate_person_id": person_id,
                        "owner_person_id": owner_person_id,
                    }
                    for person_id, owner_person_id in self._blocked_owners.items()
                ]
            )
        return _Rows(
            [
                {
                    "input_index": 0,
                    "fanout": len(self._person_ids),
                    "person_ids": self._person_ids,
                }
            ]
        )


class _NameMismatchTx:
    def run(self, query: str, **_kwargs: object) -> _Rows:
        if "is_locked" in query:
            return _Rows([{"is_locked": False}])
        if "owner_person_id" in query:
            return _Rows([])
        if "AS person_id" in query and "LIMIT 1" in query:
            return _Rows([])
        if "conflicting_value" in query:
            return _Rows([])
        if "[rel:IDENTIFIED_BY]->" in query:
            return _Rows(
                [
                    {
                        "identifier_type": "phone",
                        "normalized_value": "+6591234567",
                        "is_verified": False,
                        "last_confirmed_at": None,
                    }
                ]
            )
        if "[f:HAS_FACT]->" in query:
            return _Rows(
                [
                    {
                        "attribute_name": "full_name",
                        "attribute_value": "Unrelated Person",
                        "source_trust_tier": 1,
                        "observed_at": None,
                        "quality_flag": "valid",
                    }
                ]
            )
        if "[rel:LIVES_AT]->" in query:
            return _Rows([])
        if "AS fanout" in query:
            return _Rows([{"fanout": 1}])
        return _Rows([])


def _deal_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-5",
        record_type=RecordType.CRM_DEAL,
        observed_at="2026-08-24T00:00:00Z",
        record_hash="hash",
        raw_payload={"primary_contact_id": "contact-1"},
    )


def _contact_identifier() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="crm_contact_id",
            normalized_value="contact-1",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_normal_contact_channels_are_unverified_matching_hints() -> None:
    evidence = crm_contact_identity_evidence(
        CrmContact(
            id="123",
            full_name="Ada Lovelace",
            phones=("+6591234567",),
            emails=("ada@example.com",),
        )
    )

    assert evidence.identifiers == (
        {"type": "crm_contact_id", "value": "123", "is_verified": True},
        {"type": "phone", "value": "+6591234567", "is_verified": False},
        {"type": "email", "value": "ada@example.com", "is_verified": False},
    )
    assert evidence.metadata["identity_policy_version"] == CRM_DEAL_IDENTITY_POLICY_VERSION
    assert evidence.metadata["channel_hints_suppressed"] is False


def test_oversized_contact_suppresses_every_channel_hint() -> None:
    evidence = crm_contact_identity_evidence(
        CrmContact(
            id="123",
            full_name=None,
            phones=tuple(f"+65912345{index:02d}" for index in range(MAX_CRM_CONTACT_PHONES + 1)),
            emails=tuple(
                f"person{index}@example.com" for index in range(MAX_CRM_CONTACT_EMAILS + 1)
            ),
        )
    )

    assert evidence.identifiers == (
        {"type": "crm_contact_id", "value": "123", "is_verified": True},
    )
    assert evidence.metadata["channel_hints_suppressed"] is True
    assert evidence.metadata["channel_hint_suppression_reasons"] == [
        "phone_cardinality_exceeded",
        "email_cardinality_exceeded",
    ]


def test_canonical_contact_owner_beats_generic_channel_matching() -> None:
    result = resolve_canonical_crm_contact(
        cast(ManagedTransaction, _Tx(["person-canonical"])),
        _deal_envelope(),
        _contact_identifier(),
    )

    assert result is not None
    assert result.decision is MatchDecision.MERGE
    assert result.matched_person_id == "person-canonical"
    assert result.reasons == ["canonical_crm_contact_id"]


def test_changed_canonical_contact_owner_requires_reassignment_review() -> None:
    result = resolve_canonical_crm_contact(
        cast(ManagedTransaction, _Tx(["person-new"])),
        _deal_envelope(),
        _contact_identifier(),
        continuity_person_id="person-old",
    )

    assert result is not None
    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "person-old"
    assert result.proposed_person_id == "person-new"
    assert result.reasons == ["changed_canonical_crm_contact_requires_review"]


def test_duplicate_canonical_contact_owner_requires_review() -> None:
    result = resolve_canonical_crm_contact(
        cast(ManagedTransaction, _Tx(["person-b", "person-a"])),
        _deal_envelope(),
        _contact_identifier(),
    )

    assert result is not None
    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "person-a"
    assert result.review_candidate_person_ids == ["person-a", "person-b"]
    assert result.feature_snapshot["canonical_crm_contact_candidate_ids"] == [
        "person-a",
        "person-b",
    ]


def test_duplicate_canonical_contact_review_excludes_locked_candidate() -> None:
    result = resolve_canonical_crm_contact(
        cast(
            ManagedTransaction,
            _Tx(
                ["person-b", "person-a"],
                blocked_owners={"person-b": "person-channel-owner"},
            ),
        ),
        _deal_envelope(),
        _contact_identifier(),
    )

    assert result is not None
    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "person-a"
    assert result.review_candidate_person_ids == ["person-a"]
    assert result.feature_snapshot["blocked_canonical_crm_contact_candidate_ids"] == [
        "person-b"
    ]


def test_multi_contact_duplicate_owners_create_actionable_review() -> None:
    pipeline = IngestPipeline(cast(object, MagicMock()))
    envelope = _deal_envelope().model_copy(
        update={
            "raw_payload": {
                "primary_contact_id": None,
                "crm_contact_resolution_required": True,
                "crm_contact_groups": [
                    [{"type": "crm_contact_id", "value": "contact-1", "is_verified": True}],
                    [{"type": "crm_contact_id", "value": "contact-2", "is_verified": True}],
                ],
            }
        }
    )

    def candidates(
        _tx: ManagedTransaction,
        identifiers: list[NormalizedIdentifier],
        _addresses: list[object],
    ) -> list[CandidateResult]:
        contact_id = identifiers[0].normalized_value
        assert contact_id in {"contact-1", "contact-2"}
        return [CandidateResult(person_id=person_id) for person_id in ["person-a", "person-b"]]

    with patch("src.pipeline.find_candidates", side_effect=candidates):
        result = pipeline._resolve_ambiguous_crm_deal_contacts(
            cast(ManagedTransaction, MagicMock()),
            envelope,
            continuity_person_id=None,
        )

    assert result is not None
    assert result.decision is MatchDecision.REVIEW
    assert result.review_candidate_person_ids == ["person-a", "person-b"]
    assert result.reasons == ["ambiguous_multi_contact_crm_owners"]


def test_canonical_contact_owner_respects_active_no_match_lock() -> None:
    result = resolve_canonical_crm_contact(
        cast(
            ManagedTransaction,
            _Tx(
                ["person-canonical"],
                blocked_owners={"person-canonical": "person-channel-owner"},
            ),
        ),
        _deal_envelope(),
        _contact_identifier(),
    )

    assert result is not None
    assert crm_deal_requires_quarantine(result) is True
    assert result.reasons == ["canonical_crm_contact_owner_blocked_by_no_match_lock"]


def test_generic_crm_owner_change_always_requires_review() -> None:
    result = apply_crm_deal_match_policy(
        _deal_envelope(),
        MatchResult(
            decision=MatchDecision.MERGE,
            confidence=0.95,
            matched_person_id="person-new",
        ),
        continuity_person_id="person-old",
    )

    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "person-old"
    assert result.proposed_person_id == "person-new"
    assert "generic_crm_owner_change_requires_review" in result.reasons


def test_crm_multi_merge_becomes_review_without_extra_person_links() -> None:
    result = apply_crm_deal_match_policy(
        _deal_envelope(),
        MatchResult(
            decision=MatchDecision.MERGE,
            confidence=1.0,
            matched_person_id="person-b",
            additional_linked_person_ids=["person-c", "person-a"],
        ),
    )

    assert result.decision is MatchDecision.REVIEW
    assert result.matched_person_id == "person-a"
    assert result.additional_linked_person_ids == []
    assert result.review_candidate_person_ids == [
        "person-a",
        "person-b",
        "person-c",
    ]
    assert result.feature_snapshot["crm_deal_merge_candidate_ids"] == [
        "person-a",
        "person-b",
        "person-c",
    ]


def test_crm_channel_identifiers_are_never_projected_to_a_person() -> None:
    identifiers = [
        *_contact_identifier(),
        NormalizedIdentifier(
            identifier_type="phone",
            normalized_value="+6591234567",
            quality_flag=QualityFlag.VALID,
        ),
        NormalizedIdentifier(
            identifier_type="email",
            normalized_value="ada@example.com",
            quality_flag=QualityFlag.VALID,
        ),
    ]

    projected = projected_identifiers(_deal_envelope(), identifiers)

    assert [item.identifier_type for item in projected] == ["crm_contact_id"]


def test_non_crm_record_keeps_generic_multi_match_and_channel_projection() -> None:
    identity = _deal_envelope().model_copy(update={"record_type": RecordType.IDENTITY})
    multi_match = MatchResult(
        decision=MatchDecision.MERGE,
        confidence=1.0,
        matched_person_id="person-a",
        additional_linked_person_ids=["person-b"],
    )
    identifiers = [
        NormalizedIdentifier(
            identifier_type="phone",
            normalized_value="+6591234567",
            quality_flag=QualityFlag.VALID,
        )
    ]

    assert apply_crm_deal_match_policy(identity, multi_match) is multi_match
    assert projected_identifiers(identity, identifiers) == identifiers


def test_deal_envelope_hashes_policy_and_cannot_match_whatsapp_group_emails() -> None:
    contact = CrmContact(
        id="123",
        full_name="Ada Lovelace",
        phones=("+6591234567",),
        emails=("120363349430463692@g.us",),
    )
    record = build_deal_envelope(
        CrmDeal(
            id="456",
            title="Safe deal",
            category_id="2",
            stage_id="NEW",
            observed_at=datetime(2026, 8, 24, tzinfo=UTC),
            primary_contact=contact,
            contacts=(contact,),
            contact_count=1,
            has_ambiguous_contacts=False,
            raw_payload={"ID": "456"},
        ),
        "eko",
    )
    envelope = SourceRecordEnvelope(source_system="bitrix_chat", **record)

    normalized = normalize_envelope_identifiers(envelope)

    assert record["raw_payload"]["crm_deal_identity_policy_version"] == (
        CRM_DEAL_IDENTITY_POLICY_VERSION
    )
    assert record["record_hash"]
    assert [item.identifier_type for item in normalized] == ["crm_contact_id", "phone"]
    assert record["raw_payload"]["crm_contact_raw_groups"] == [
        [
            {"type": "crm_contact_id", "value": "123", "is_verified": True},
            {"type": "phone", "value": "+6591234567", "is_verified": False},
            {
                "type": "email",
                "value": "120363349430463692@g.us",
                "is_verified": False,
            },
        ]
    ]


def test_oversized_raw_channel_change_changes_deal_hash() -> None:
    def build(email_suffix: str) -> dict[str, object]:
        contact = CrmContact(
            id="123",
            full_name=None,
            phones=(),
            emails=tuple(
                f"person{index}-{email_suffix}@example.com"
                for index in range(MAX_CRM_CONTACT_EMAILS + 1)
            ),
        )
        return build_deal_envelope(
            CrmDeal(
                id="456",
                title="Oversized contact deal",
                category_id="2",
                stage_id="NEW",
                observed_at=datetime(2026, 8, 24, tzinfo=UTC),
                primary_contact=contact,
                contacts=(contact,),
                contact_count=1,
                has_ambiguous_contacts=False,
                raw_payload={"ID": "456"},
            ),
            "eko",
        )

    first = build("first")
    second = build("second")

    assert (
        first["identifiers"]
        == second["identifiers"]
        == [{"type": "crm_contact_id", "value": "123", "is_verified": True}]
    )
    assert first["record_hash"] != second["record_hash"]


def test_blocked_canonical_owner_persists_durable_pending_review() -> None:
    pipeline = IngestPipeline(cast(object, MagicMock()))
    envelope = _deal_envelope().model_copy(update={"source_record_version": "1"})
    quarantine = MatchResult(
        decision=MatchDecision.REVIEW,
        confidence=1.0,
        reasons=["canonical_crm_contact_owner_blocked_by_no_match_lock"],
        matched_person_id="person-a",
        proposed_person_id="person-a",
        review_candidate_person_ids=["person-a"],
        feature_snapshot={"crm_deal_quarantine": True},
    )
    tx = cast(ManagedTransaction, MagicMock())

    with ExitStack() as stack:
        stack.enter_context(patch("src.pipeline.find_candidates", return_value=[]))
        stack.enter_context(
            patch("src.pipeline.resolve_canonical_crm_contact", return_value=quarantine)
        )
        upsert = stack.enter_context(patch("src.pipeline.upsert_nodes"))
        persist = stack.enter_context(
            patch("src.pipeline.persist_source_record", return_value="deal-sr")
        )
        persist_decision = stack.enter_context(
            patch("src.pipeline.persist_match_decision", return_value="decision-1")
        )
        create_review = stack.enter_context(
            patch("src.pipeline.create_review_case_if_needed", return_value="review-1")
        )
        link = stack.enter_context(patch("src.pipeline.link_record_to_graph"))

        result = pipeline._execute_ingest(tx, envelope, _contact_identifier(), [], [])

    assert result.dropped is False
    assert result.source_record_pk == "deal-sr"
    assert result.match_decision_id == "decision-1"
    assert result.review_case_id == "review-1"
    assert result.person_id is None
    assert result.candidate_count == 1
    upsert.assert_not_called()
    link.assert_not_called()
    assert persist.call_args.kwargs["lifecycle_status"].value == "pending_review"
    persist_decision.assert_called_once_with(tx, quarantine, "deal-sr")
    create_review.assert_called_once_with(tx, quarantine, "decision-1")


def test_crm_multi_merge_persists_provisional_link_without_identity_evidence() -> None:
    pipeline = IngestPipeline(cast(object, MagicMock()))
    pipeline._match_engine = MagicMock(
        evaluate=MagicMock(
            return_value=MatchResult(
                decision=MatchDecision.MERGE,
                confidence=1.0,
                matched_person_id="person-a",
                additional_linked_person_ids=["person-b"],
            )
        )
    )
    envelope = _deal_envelope().model_copy(
        update={
            "raw_payload": {},
            "source_record_version": "1",
        }
    )
    identifiers = [
        NormalizedIdentifier(
            identifier_type="crm_contact_id",
            normalized_value="contact-1",
            quality_flag=QualityFlag.VALID,
        ),
        NormalizedIdentifier(
            identifier_type="phone",
            normalized_value="+6591234567",
            quality_flag=QualityFlag.VALID,
        ),
    ]
    tx = cast(ManagedTransaction, MagicMock())

    with ExitStack() as stack:
        find = stack.enter_context(patch("src.pipeline.find_candidates", return_value=[]))
        upsert = stack.enter_context(patch("src.pipeline.upsert_nodes"))
        persist = stack.enter_context(
            patch("src.pipeline.persist_source_record", return_value="deal-sr")
        )
        stack.enter_context(patch("src.pipeline.persist_match_decision", return_value="decision-1"))
        stack.enter_context(
            patch("src.pipeline.create_review_case_if_needed", return_value="review-1")
        )
        link = stack.enter_context(patch("src.pipeline.link_record_to_graph"))
        activate = stack.enter_context(patch("src.pipeline.activate_staged_version"))
        audit = stack.enter_context(patch("src.pipeline.audit_person_pairs"))
        stack.enter_context(patch("src.pipeline.mark_profile_analysis_dirty"))
        stack.enter_context(patch("src.pipeline.record_auto_merge_event"))
        stack.enter_context(patch("src.pipeline.materialize_bankruptcy_case"))
        stack.enter_context(patch.object(pipeline, "_write_chat_vehicle_observations"))

        result = pipeline._execute_ingest(tx, envelope, identifiers, [], [])

    assert result.match_decision is MatchDecision.REVIEW
    assert result.person_id == "person-a"
    find.assert_called_once()
    assert upsert.call_args.args[1] == [identifiers[0]]
    assert persist.call_args.kwargs["identifiers"] == [identifiers[0]]
    assert len(link.call_args_list) == 1
    assert link.call_args.kwargs["person_id"] == "person-a"
    assert link.call_args.kwargs["identifiers"] == [identifiers[0]]
    assert link.call_args.kwargs["attach_evidence"] is False
    activate.assert_not_called()
    audit.assert_not_called()


def test_crm_channel_match_with_strong_name_mismatch_never_auto_merges() -> None:
    result = MatchEngine().evaluate(
        cast(ManagedTransaction, _NameMismatchTx()),
        [CandidateResult(person_id="person-a")],
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234567",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [
            NormalizedAttribute(
                attribute_name="full_name",
                attribute_value="Ada Lovelace",
                quality_flag=QualityFlag.VALID,
            )
        ],
        record_type=RecordType.CRM_DEAL,
    )

    assert result.decision is not MatchDecision.MERGE
    assert result.additional_linked_person_ids == []
    assert result.is_new_person is True
