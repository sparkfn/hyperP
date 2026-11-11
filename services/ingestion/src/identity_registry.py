"""Identity type policies shared by ingestion and shadow assertions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.graph.bootstrap import PROMOTES_PERSON_SOURCE_KEYS


@dataclass(frozen=True)
class IdentifierTypePolicy:
    """Seed policy for identifier candidate generation and future identity work."""

    candidate_eligible: bool
    single_valued: bool
    fanout_cap: int | None
    half_life_days: int | None
    promotes: bool


_DEFAULT_POLICY = IdentifierTypePolicy(
    candidate_eligible=False,
    single_valued=False,
    fanout_cap=None,
    half_life_days=None,
    promotes=False,
)
_SOCIAL_POLICY = IdentifierTypePolicy(
    candidate_eligible=True,
    single_valued=False,
    fanout_cap=25,
    half_life_days=None,
    promotes=False,
)

# No identifier aging configuration exists yet; all half_life_days remain None.
# `promotes` is a future C3 seed only and is not consumed by C1 ingestion.
IDENTIFIER_TYPE_REGISTRY: Mapping[str, IdentifierTypePolicy] = {
    "phone": IdentifierTypePolicy(True, False, 50, None, False),
    "email": IdentifierTypePolicy(True, False, 100, None, False),
    "nric": IdentifierTypePolicy(True, True, 5, None, True),
    "device_id": IdentifierTypePolicy(True, False, 25, None, False),
    "social:*": _SOCIAL_POLICY,
    "crm_contact_id": IdentifierTypePolicy(True, True, None, None, True),
    "crm_lead_id": IdentifierTypePolicy(True, True, None, None, True),
    "crm_company_id": IdentifierTypePolicy(True, True, None, None, True),
    "membership_id": IdentifierTypePolicy(True, False, None, None, False),
    "external:bitrix": IdentifierTypePolicy(True, False, None, None, False),
    "external_customer_id": IdentifierTypePolicy(True, False, None, None, False),
}

FACT_ASSERTION_ATTRIBUTES = frozenset({"full_name", "dob"})


def identifier_policy(identifier_type: str) -> IdentifierTypePolicy:
    """Return a known type's policy, shared social policy, or safe default."""
    policy = IDENTIFIER_TYPE_REGISTRY.get(identifier_type)
    if policy is not None:
        return policy
    if identifier_type.startswith("social:"):
        return _SOCIAL_POLICY
    return _DEFAULT_POLICY


def source_promotes_person(source_key: str) -> bool:
    """Return whether the source's bootstrap seed permits person promotion."""
    return source_key in PROMOTES_PERSON_SOURCE_KEYS
