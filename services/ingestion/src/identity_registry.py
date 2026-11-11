"""Identity type policies shared by ingestion and shadow assertions.

Unseeded values (no source of truth):
- Per-type half-life windows: docs/profile-unifier-architecture.md:638-647
  requires per-type configurable aging windows, but no aging implementation
  or configuration exists in code today; all half_life_days remain None.
- Trust-tier-derived promotion: bootstrap trust tiers (src.graph.bootstrap)
  govern attribute fact precedence, not identifier promotion. `promotes` is
  only True where existing code makes that type decisive.
- `single_valued` for CRM ids (`crm_*_id`) and `promotes` for `crm_contact_id`:
  CRM owner resolution is record-type specific (src.pipeline_crm_identity) and
  no generic rule exists, so both stay False until one is specified.
"""

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


# Default policy for unknown/unregistered identifier types. Candidate generation
# in src.graph.queries.matching has no type filter, so candidate_eligible=True.
_DEFAULT_POLICY = IdentifierTypePolicy(
    candidate_eligible=True,
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

# Registry of known identifier types.
# - single_valued: True where code treats a second distinct value as a conflict
#   (nric: `_check_government_id`, matching/deterministic.py:206-230).
# - promotes: True where existing code makes this identifier type decisive for
#   an automatic merge (nric: matching/deterministic.py:206-230;
#   external_customer_id and membership_id: TRUSTED_ID_TYPES,
#   matching/deterministic.py:90,267).
IDENTIFIER_TYPE_REGISTRY: Mapping[str, IdentifierTypePolicy] = {
    "phone": IdentifierTypePolicy(True, False, 50, None, False),
    "email": IdentifierTypePolicy(True, False, 100, None, False),
    "nric": IdentifierTypePolicy(True, True, 5, None, True),
    "device_id": IdentifierTypePolicy(True, False, 25, None, False),
    "social:*": _SOCIAL_POLICY,
    "crm_contact_id": IdentifierTypePolicy(True, False, None, None, False),
    "crm_lead_id": IdentifierTypePolicy(True, False, None, None, False),
    "crm_company_id": IdentifierTypePolicy(True, False, None, None, False),
    "membership_id": IdentifierTypePolicy(True, False, None, None, True),
    "external:bitrix": IdentifierTypePolicy(True, False, None, None, False),
    "external_customer_id": IdentifierTypePolicy(True, False, None, None, True),
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
