"""Compatibility entry point for ingestion golden-profile computation."""

from __future__ import annotations

import logging
from typing import Protocol, TypedDict, runtime_checkable

from neo4j import ManagedTransaction, Record

from src import golden_profile_active_authority as _active_authority
from src.golden_profile_authority_models import (
    GoldenProfileFields,
    GoldenProfileRecomputeResult,
)
from src.graph import queries

recompute_golden_profile_from_active_authority = (
    _active_authority.recompute_golden_profile_from_active_authority
)
derive_golden_profile_from_active_authority = (
    _active_authority.derive_golden_profile_from_active_authority
)

logger = logging.getLogger(__name__)

__all__ = (
    "GoldenProfileRecomputeResult",
    "compute_golden_profile",
    "derive_golden_profile_from_active_authority",
    "recompute_golden_profile_from_active_authority",
)

_TRUST_TIER_RANK: dict[str, int] = {
    "tier_1": 4,
    "tier_2": 3,
    "tier_3": 2,
    "tier_4": 1,
}
_GOLDEN_PROFILE_VERSION = "v0.1.0"


class _LegacyFact(TypedDict):
    attribute_name: str
    attribute_value: str
    quality_flag: str | None
    source_trust_tier: str | None
    observed_at: str | None


class _LegacyIdentifier(TypedDict):
    identifier_type: str
    normalized_value: str
    is_verified: bool
    last_confirmed_at: str | None


class _LegacyAddress(TypedDict):
    address_id: str
    is_verified: bool
    last_confirmed_at: str | None


class _LegacyGoldenProfile(GoldenProfileFields):
    person_id: str


def compute_golden_profile(tx: ManagedTransaction, person_id: str) -> _LegacyGoldenProfile:
    """Compute and persist the legacy profile using its established survivorship rules."""
    facts, identifiers, addresses = _fetch_person_evidence(tx, person_id)
    fields = _apply_survivorship(facts, identifiers, addresses)
    profile: _LegacyGoldenProfile = {"person_id": person_id, **fields}
    tx.run(
        queries.UPDATE_GOLDEN_PROFILE,
        person_id=person_id,
        preferred_full_name=fields["preferred_full_name"],
        preferred_dob=fields["preferred_dob"],
        preferred_phone=fields["preferred_phone"],
        preferred_email=fields["preferred_email"],
        preferred_address_id=fields["preferred_address_id"],
        preferred_nric=fields["preferred_nric"],
        preferred_race_ethnicity=fields["preferred_race_ethnicity"],
        profile_completeness_score=fields["profile_completeness_score"],
        golden_profile_version=fields["golden_profile_version"],
    )
    logger.info(
        "Golden profile computed for person %s (completeness=%.2f)",
        person_id,
        profile["profile_completeness_score"],
    )
    return profile


def _fetch_person_evidence(
    tx: ManagedTransaction, person_id: str
) -> tuple[list[_LegacyFact], list[_LegacyIdentifier], list[_LegacyAddress]]:
    return (
        [_legacy_fact(row) for row in tx.run(queries.FETCH_PERSON_FACTS, person_id=person_id)],
        [
            _legacy_identifier(row)
            for row in tx.run(queries.FETCH_PERSON_IDENTIFIERS, person_id=person_id)
        ],
        [
            _legacy_address(row)
            for row in tx.run(queries.FETCH_PERSON_ADDRESSES, person_id=person_id)
        ],
    )


def _legacy_fact(row: Record) -> _LegacyFact:
    return {
        "attribute_name": _row_string(row, "attribute_name"),
        "attribute_value": _row_string(row, "attribute_value"),
        "quality_flag": _row_optional_string(row, "quality_flag"),
        "source_trust_tier": _row_optional_string(row, "source_trust_tier"),
        "observed_at": _row_optional_string(row, "observed_at"),
    }


def _legacy_identifier(row: Record) -> _LegacyIdentifier:
    return {
        "identifier_type": _row_string(row, "identifier_type"),
        "normalized_value": _row_string(row, "normalized_value"),
        "is_verified": _row_bool(row, "is_verified"),
        "last_confirmed_at": _row_optional_string(row, "last_confirmed_at"),
    }


def _legacy_address(row: Record) -> _LegacyAddress:
    return {
        "address_id": _row_string(row, "address_id"),
        "is_verified": _row_bool(row, "is_verified"),
        "last_confirmed_at": _row_optional_string(row, "last_confirmed_at"),
    }


def _apply_survivorship(
    facts: list[_LegacyFact],
    identifiers: list[_LegacyIdentifier],
    addresses: list[_LegacyAddress],
) -> GoldenProfileFields:
    preferred_full_name = _pick_best_fact(facts, "full_name")
    if preferred_full_name is None:
        preferred_full_name = _pick_best_fact(facts, "preferred_name") or _pick_best_fact(
            facts, "legal_name"
        )
    dob = _pick_best_fact(facts, "dob")
    phone = _pick_best_identifier(identifiers, "phone")
    email = _pick_best_identifier(identifiers, "email")
    address_id = _pick_best_address(addresses)
    return {
        "preferred_full_name": preferred_full_name,
        "preferred_dob": dob,
        "preferred_phone": phone,
        "preferred_email": email,
        "preferred_address_id": address_id,
        "preferred_nric": _pick_best_identifier(identifiers, "nric"),
        "preferred_race_ethnicity": _pick_best_fact(facts, "race_ethnicity"),
        "profile_completeness_score": round(
            sum(value is not None for value in (preferred_full_name, dob, phone, email, address_id))
            / 5,
            2,
        ),
        "golden_profile_version": _GOLDEN_PROFILE_VERSION,
    }


def _pick_best_fact(facts: list[_LegacyFact], attribute_name: str) -> str | None:
    matching = [fact for fact in facts if fact["attribute_name"] == attribute_name]
    if not matching:
        return None
    return max(
        matching,
        key=lambda fact: (
            fact["quality_flag"] == "valid",
            _TRUST_TIER_RANK.get(fact["source_trust_tier"] or "", 0),
            fact["observed_at"] or "",
        ),
    )["attribute_value"]


def _pick_best_identifier(identifiers: list[_LegacyIdentifier], identifier_type: str) -> str | None:
    matching = [item for item in identifiers if item["identifier_type"] == identifier_type]
    if not matching:
        return None
    return max(
        matching,
        key=lambda item: (item["is_verified"], item["last_confirmed_at"] or ""),
    )["normalized_value"]


def _pick_best_address(addresses: list[_LegacyAddress]) -> str | None:
    if not addresses:
        return None
    return max(
        addresses,
        key=lambda item: (item["is_verified"], item["last_confirmed_at"] or ""),
    )["address_id"]


def _row_string(row: Record, key: str) -> str:
    value: object = row[key]
    if not isinstance(value, str):
        raise ValueError("golden profile string evidence is malformed")
    return value


def _row_optional_string(row: Record, key: str) -> str | None:
    value: object = row[key]
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, _IsoFormatValue):
        return value.iso_format()
    raise ValueError("golden profile optional evidence is malformed")


@runtime_checkable
class _IsoFormatValue(Protocol):
    """Narrow boundary for Neo4j temporal values returned by legacy reads."""

    def iso_format(self) -> str: ...


def _row_bool(row: Record, key: str) -> bool:
    value: object = row[key]
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    raise ValueError("golden profile boolean evidence is malformed")
