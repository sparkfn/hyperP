"""Pure coverage of the ordered NRIC Person-anchor decision policy."""

from __future__ import annotations

import json
import secrets
from dataclasses import replace

from _nric_rehash_apply_builders import _canonical
from src.graph.bootstrap import SOURCE_NRIC_TRUST
from src.identity_link_member_evidence import member_evidence
from src.identity_registry import PROMOTION_MIN_TRUST_TIERS, source_promotes_person
from src.models import RecordType
from src.normalizers.nric import nric_digest
from src.nric_rehash_anchor_decide import (
    AnchorEvidence,
    AnchorIdentifier,
    AnchorSourceRecord,
    decide_anchor,
)


def _digests() -> tuple[str, str]:
    key = secrets.token_hex(32)
    first_canonical = _canonical()
    second_canonical = _canonical({first_canonical})
    return nric_digest(first_canonical, key), nric_digest(second_canonical, key)


def _source_key(*, promoting: bool) -> str:
    matches = sorted(
        source_key
        for source_key, trust_tier in SOURCE_NRIC_TRUST.items()
        if source_promotes_person(source_key)
        and (trust_tier in PROMOTION_MIN_TRUST_TIERS) is promoting
    )
    if not matches:
        raise AssertionError("no configured NRIC source matches the requested gate")
    return matches[0]


def _source_record(
    source_key: str,
    *,
    record_type: RecordType = RecordType.IDENTITY,
    name: str | None = "Generated Person",
    dob: str | None = "1985-01-02",
) -> AnchorSourceRecord:
    attributes: list[dict[str, str]] = []
    if name is not None:
        attributes.append(
            {
                "attribute_name": "full_name",
                "attribute_value": name,
                "quality_flag": "valid",
            }
        )
    if dob is not None:
        attributes.append(
            {
                "attribute_name": "dob",
                "attribute_value": dob,
                "quality_flag": "valid",
            }
        )
    return AnchorSourceRecord(
        source_record_pk=secrets.token_hex(16),
        source_key=source_key,
        record_type=record_type.value,
        normalized_payload=json.dumps({"attributes": attributes}, sort_keys=True),
    )


def _evidence(
    digest: str,
    *,
    source_records: tuple[AnchorSourceRecord, ...] | None = None,
    nric_key: str | None = None,
    has_anchor: bool = False,
    checksum_valid: bool | None = True,
    other_key_holders: int = 0,
    other_active_evidence: int = 0,
    evidence_truncated: bool = False,
    identifier_count: int = 1,
) -> AnchorEvidence:
    identifiers = tuple(
        AnchorIdentifier(
            identifier_id=secrets.token_hex(16),
            normalized_value=digest,
            checksum_valid=checksum_valid,
            other_key_holders=other_key_holders,
            other_active_evidence=other_active_evidence,
        )
        for _ in range(identifier_count)
    )
    records = source_records
    if records is None:
        records = (_source_record(_source_key(promoting=True)),)
    return AnchorEvidence(
        person_id=secrets.token_hex(16),
        nric_key=nric_key,
        has_anchor=has_anchor,
        identifiers=identifiers,
        records=records,
        evidence_truncated=evidence_truncated,
    )


def test_anchor_decision_accepts_one_consistent_promoting_identity() -> None:
    digest, _other_digest = _digests()

    decision = decide_anchor(_evidence(digest), "generated-version", ())

    assert decision.kind == "anchor"
    assert decision.reason == "anchor"


def test_anchor_decision_early_safety_rules_take_precedence() -> None:
    digest, other_digest = _digests()
    base = _evidence(digest)
    unrecoverable = replace(
        base,
        identifiers=(replace(base.identifiers[0], normalized_value=None),),
    )
    already = _evidence(
        digest,
        nric_key=digest,
        has_anchor=True,
        checksum_valid=False,
        other_key_holders=1,
        evidence_truncated=True,
        source_records=(),
    )
    cases = (
        (
            _evidence(
                digest,
                identifier_count=2,
                nric_key=other_digest,
                checksum_valid=False,
            ),
            "handoff",
            "multi_nric",
        ),
        (unrecoverable, "handoff", "unrecoverable_identifier"),
        (already, "already_anchored", "already_anchored"),
        (
            _evidence(digest, nric_key=other_digest, checksum_valid=False),
            "handoff",
            "person_key_conflict",
        ),
    )
    for evidence, expected_kind, expected_reason in cases:
        decision = decide_anchor(evidence, "generated-version", ())
        assert decision.kind == expected_kind
        assert decision.reason == expected_reason


def test_anchor_decision_checksum_shared_truncated_and_missing_context_order() -> None:
    digest, _other_digest = _digests()
    cases = (
        (
            _evidence(digest, checksum_valid=False, other_key_holders=1),
            "checksum_invalid",
        ),
        (
            _evidence(digest, other_active_evidence=1, evidence_truncated=True),
            "shared_owner",
        ),
        (
            _evidence(digest, evidence_truncated=True, source_records=()),
            "evidence_truncated",
        ),
        (_evidence(digest, source_records=()), "missing_source_context"),
    )
    for evidence, expected_reason in cases:
        decision = decide_anchor(evidence, "generated-version", ())
        assert decision.kind == "handoff"
        assert decision.reason == expected_reason


def test_anchor_decision_uses_real_registry_gate_and_conversation_rule() -> None:
    digest, _other_digest = _digests()
    rejected_record = _source_record(_source_key(promoting=False))
    rejected = decide_anchor(
        _evidence(digest, source_records=(rejected_record,)), "generated-version", ()
    )
    assert rejected.kind == "handoff"
    assert rejected.reason in {"source_not_promoting", "below_tier_floor"}

    conversation = _source_record(
        _source_key(promoting=True),
        record_type=RecordType.CONVERSATION,
    )
    conversation_decision = decide_anchor(
        _evidence(digest, source_records=(conversation,)), "generated-version", ()
    )
    assert conversation_decision.kind == "handoff"
    assert conversation_decision.reason == "conversation_record"


def test_anchor_decision_hands_off_member_and_durable_holder_contradictions() -> None:
    digest, _other_digest = _digests()
    first = _source_record(_source_key(promoting=True), dob="1985-01-02")
    conflicting = _source_record(_source_key(promoting=True), dob="1991-04-05")
    contradiction = decide_anchor(
        _evidence(digest, source_records=(first, conflicting)), "generated-version", ()
    )
    assert contradiction.kind == "handoff"
    assert contradiction.reason == "nric_contradiction"

    holder = member_evidence(secrets.token_hex(16), ["Other Generated Person"], ["1991-04-05"], [])
    durable_contradiction = decide_anchor(
        _evidence(digest, source_records=(first,)), "generated-version", (holder,)
    )
    assert durable_contradiction.kind == "handoff"
    assert durable_contradiction.reason == "nric_contradiction"


def test_gate_reason_comes_from_first_rejected_record_in_key_order() -> None:
    digest, _other_digest = _digests()
    records = tuple(
        sorted(
            (
                _source_record(
                    _source_key(promoting=True),
                    record_type=RecordType.CONVERSATION,
                ),
                _source_record(_source_key(promoting=False)),
            ),
            key=lambda item: item.source_record_pk,
        )
    )
    expected = decide_anchor(_evidence(digest, source_records=records[:1]), "generated-version", ())
    actual = decide_anchor(_evidence(digest, source_records=records), "generated-version", ())
    assert actual.kind == "handoff"
    assert actual.reason == expected.reason
