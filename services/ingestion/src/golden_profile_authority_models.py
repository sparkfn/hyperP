"""Typed records shared by active-authority golden-profile recomputation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict


class AuthorityFact(TypedDict):
    attribute_name: str
    attribute_value: str
    source_trust_tier: str | None
    observed_at: str | None
    quality_flag: str | None
    source_record_pk: str | None


class AuthorityIdentifier(TypedDict):
    identifier_type: str
    normalized_value: str
    is_verified: bool
    last_confirmed_at: str | None
    source_record_pk: str | None


class AuthorityAddress(TypedDict):
    address_id: str
    normalized_full: str | None
    is_verified: bool
    last_confirmed_at: str | None
    source_record_pk: str | None


class GoldenProfileFields(TypedDict):
    preferred_full_name: str | None
    preferred_dob: str | None
    preferred_phone: str | None
    preferred_email: str | None
    preferred_address_id: str | None
    preferred_nric: str | None
    preferred_race_ethnicity: str | None
    profile_completeness_score: float
    golden_profile_version: str


GoldenFieldKey = Literal[
    "preferred_full_name",
    "preferred_dob",
    "preferred_phone",
    "preferred_email",
    "preferred_address_id",
    "preferred_nric",
    "preferred_race_ethnicity",
]


@dataclass(frozen=True)
class OverrideEntry:
    field: str
    custom_value: str | None
    source_record_pk: str | None
    malformed: bool
    unknown: bool


@dataclass(frozen=True)
class GoldenProfileRecomputeResult:
    person_id: str
    changed: bool
    conflict_fields: tuple[str, ...]
    profile: GoldenProfileFields
    current_profile: GoldenProfileFields
