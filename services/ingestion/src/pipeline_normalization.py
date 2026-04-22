"""Normalization helpers and fanout caps used by the ingest pipeline.

Free functions instead of methods so they're trivially testable in isolation
and don't carry pipeline state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.models import (
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    SourceRecordEnvelope,
)
from src.normalizers.address import normalize_address
from src.normalizers.email import normalize_email
from src.normalizers.name import normalize_name
from src.normalizers.phone import normalize_phone

logger = logging.getLogger(__name__)

#: Normalizer signature shared by phone/email/name/etc.
NormalizerFn = Callable[[str], tuple[str | None, QualityFlag]]

# Registry: identifier_type -> normalizer.
_IDENTIFIER_NORMALIZERS: dict[str, NormalizerFn] = {
    "phone": normalize_phone,
    "email": normalize_email,
}

# Registry: attribute_name -> normalizer.
_ATTRIBUTE_NORMALIZERS: dict[str, NormalizerFn] = {
    "full_name": normalize_name,
    "preferred_name": normalize_name,
    "legal_name": normalize_name,
}

#: Default cardinality cap for any ``social:*`` identifier type without an
#: explicit entry in :data:`_FANOUT_CAPS`.
_DEFAULT_SOCIAL_FANOUT_CAP = 25

#: Per-identifier-type cardinality caps used during candidate generation.
#: Identifiers whose fanout exceeds the cap are skipped (see CLAUDE.md policy).
_FANOUT_CAPS: dict[str, int] = {
    "phone": 50,
    "email": 100,
    "nric": 5,
    "device_id": 25,
    "social:facebook": _DEFAULT_SOCIAL_FANOUT_CAP,
    "social:google": _DEFAULT_SOCIAL_FANOUT_CAP,
    "social:apple": _DEFAULT_SOCIAL_FANOUT_CAP,
}

# Attributes handled outside the normalizer registry.
_SKIP_ATTRIBUTES = frozenset({"address"})


def fanout_cap_for(identifier_type: str) -> int | None:
    cap = _FANOUT_CAPS.get(identifier_type)
    if cap is None and identifier_type.startswith("social:"):
        return _DEFAULT_SOCIAL_FANOUT_CAP
    return cap


def is_usable(quality_flag: QualityFlag) -> bool:
    return quality_flag in (QualityFlag.VALID, QualityFlag.PARTIAL_PARSE)


def _passthrough_normalize(raw: str) -> tuple[str | None, QualityFlag]:
    """Fallback normalizer: strip whitespace, return valid if non-empty."""
    value = raw.strip()
    return (value, QualityFlag.VALID) if value else (None, QualityFlag.INVALID_FORMAT)


def normalize_envelope_identifiers(
    envelope: SourceRecordEnvelope,
) -> list[NormalizedIdentifier]:
    results: list[NormalizedIdentifier] = []
    for raw_id in envelope.identifiers:
        id_type = raw_id.type.lower().strip()
        normalizer = _IDENTIFIER_NORMALIZERS.get(id_type, _passthrough_normalize)
        normalized, flag = normalizer(raw_id.value)
        if normalized:
            results.append(NormalizedIdentifier(
                identifier_type=id_type,
                normalized_value=normalized,
                is_verified=raw_id.is_verified,
                quality_flag=flag,
            ))
        else:
            logger.warning(
                "%s normalization failed for %s: %s",
                id_type, raw_id.value, flag,
            )
    return results


def normalize_envelope_address(
    envelope: SourceRecordEnvelope,
) -> NormalizedAddressModel | None:
    raw_address = envelope.attributes.get("address")
    if not raw_address or not isinstance(raw_address, str):
        return None

    parsed, flag = normalize_address(raw_address)
    if parsed is None:
        logger.warning("Address normalization failed for record %s: %s",
                       envelope.source_record_id, flag)
        return None

    return NormalizedAddressModel(
        unit_number=parsed.unit_number,
        street_number=parsed.street_number,
        street_name=parsed.street_name,
        building_name=parsed.building_name,
        city=parsed.city,
        state_province=parsed.state_province,
        postal_code=parsed.postal_code,
        country_code=parsed.country_code,
        normalized_full=parsed.normalized_full,
        quality_flag=flag,
    )


def normalize_envelope_attributes(
    envelope: SourceRecordEnvelope,
) -> list[NormalizedAttribute]:
    results: list[NormalizedAttribute] = []
    for attr_name, raw_value in envelope.attributes.items():
        if attr_name in _SKIP_ATTRIBUTES:
            continue
        value_str = raw_value if isinstance(raw_value, str) else str(raw_value)
        normalizer = _ATTRIBUTE_NORMALIZERS.get(attr_name, _passthrough_normalize)
        normalized, flag = normalizer(value_str)
        if normalized:
            results.append(NormalizedAttribute(
                attribute_name=attr_name,
                attribute_value=normalized,
                quality_flag=flag,
            ))
    return results
