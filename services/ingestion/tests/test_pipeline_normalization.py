"""Region-hint phone normalization fallback chain (Track A1)."""

from __future__ import annotations

from src.models import QualityFlag, RawIdentifier, SourceRecordEnvelope
from src.pipeline_normalization import normalize_envelope_identifiers


def _envelope(*raw_identifiers: RawIdentifier) -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="eko_phppos",
        source_record_id="eko_phppos-customer-1",
        observed_at="2026-06-16T00:00:00Z",
        record_hash="sha256:test",
        identifiers=list(raw_identifiers),
    )


def test_no_region_hint_behaves_as_before() -> None:
    envelope = _envelope(RawIdentifier(type="phone", value="96542555"))
    results = normalize_envelope_identifiers(envelope)
    assert results[0].normalized_value == "+6596542555"
    assert results[0].quality_flag == QualityFlag.VALID


def test_region_hint_resolves_ambiguous_local_number() -> None:
    # 8-digit local number is ambiguous between SG and MY defaults (see design
    # doc background) — the hint derived from the source row's
    # phone_code/country resolves it to the MY number.
    envelope = _envelope(RawIdentifier(type="phone", value="96542555", region_hint="MY"))
    results = normalize_envelope_identifiers(envelope)
    assert results[0].normalized_value == "+6096542555"
    assert results[0].quality_flag == QualityFlag.VALID


def test_invalid_hint_falls_back_to_sg_default() -> None:
    # A bogus region hint must never make a number that normalizes fine under
    # the SG default start failing.
    envelope = _envelope(RawIdentifier(type="phone", value="96542555", region_hint="XX"))
    results = normalize_envelope_identifiers(envelope)
    assert results[0].normalized_value == "+6596542555"
    assert results[0].quality_flag == QualityFlag.VALID


def test_non_phone_identifiers_ignore_region_hint() -> None:
    envelope = _envelope(RawIdentifier(type="email", value="Ada@Example.com", region_hint="MY"))
    results = normalize_envelope_identifiers(envelope)
    assert results[0].normalized_value == "ada@example.com"
