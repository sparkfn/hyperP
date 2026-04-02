"""Golden profile computation — survivorship rules applied to person facts.

Recomputes the preferred values for a person's golden profile by querying
all attribute facts, identifiers, and addresses, then applying survivorship
rules: verified > unverified, newer > older, higher trust tier > lower.
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import ManagedTransaction

from src.graph import queries

logger = logging.getLogger(__name__)

# Trust tier ordering (higher index = higher trust).
_TRUST_TIER_RANK: dict[str, int] = {
    "tier_1": 4,  # highest trust
    "tier_2": 3,
    "tier_3": 2,
    "tier_4": 1,  # lowest trust
}

# Golden profile version — bump when survivorship logic changes.
_GOLDEN_PROFILE_VERSION = "v0.1.0"


def compute_golden_profile(
    tx: ManagedTransaction,
    person_id: str,
) -> dict[str, Any]:
    """Compute and persist the golden profile for *person_id*.

    Parameters
    ----------
    tx:
        Active Neo4j write transaction.
    person_id:
        The person whose golden profile should be (re)computed.

    Returns
    -------
    dict
        The computed golden profile fields.
    """

    # ------------------------------------------------------------------
    # 1. Fetch attribute facts
    # ------------------------------------------------------------------
    facts_result = tx.run(queries.FETCH_PERSON_FACTS, person_id=person_id)
    facts: list[dict[str, Any]] = [dict(r) for r in facts_result]

    # ------------------------------------------------------------------
    # 2. Fetch identifiers (phone, email)
    # ------------------------------------------------------------------
    idents_result = tx.run(queries.FETCH_PERSON_IDENTIFIERS, person_id=person_id)
    identifiers: list[dict[str, Any]] = [dict(r) for r in idents_result]

    # ------------------------------------------------------------------
    # 3. Fetch addresses
    # ------------------------------------------------------------------
    addrs_result = tx.run(queries.FETCH_PERSON_ADDRESSES, person_id=person_id)
    addresses: list[dict[str, Any]] = [dict(r) for r in addrs_result]

    # ------------------------------------------------------------------
    # 4. Apply survivorship rules per attribute
    # ------------------------------------------------------------------
    preferred_full_name = _pick_best_fact(facts, "full_name")
    # Fallback to preferred_name or legal_name if full_name is absent
    if preferred_full_name is None:
        preferred_full_name = (
            _pick_best_fact(facts, "preferred_name")
            or _pick_best_fact(facts, "legal_name")
        )

    preferred_dob = _pick_best_fact(facts, "dob")
    preferred_phone = _pick_best_identifier(identifiers, "phone")
    preferred_email = _pick_best_identifier(identifiers, "email")
    preferred_address_id = _pick_best_address(addresses)

    # ------------------------------------------------------------------
    # 5. Compute completeness score (0.0 – 1.0)
    # ------------------------------------------------------------------
    completeness_fields = [
        preferred_full_name,
        preferred_phone,
        preferred_email,
        preferred_dob,
        preferred_address_id,
    ]
    filled = sum(1 for f in completeness_fields if f is not None)
    profile_completeness_score = round(filled / len(completeness_fields), 2)

    # ------------------------------------------------------------------
    # 6. Persist to Person node
    # ------------------------------------------------------------------
    tx.run(
        queries.UPDATE_GOLDEN_PROFILE,
        person_id=person_id,
        preferred_full_name=preferred_full_name,
        preferred_phone=preferred_phone,
        preferred_email=preferred_email,
        preferred_dob=preferred_dob,
        preferred_address_id=preferred_address_id,
        profile_completeness_score=profile_completeness_score,
        golden_profile_version=_GOLDEN_PROFILE_VERSION,
    )

    profile = {
        "person_id": person_id,
        "preferred_full_name": preferred_full_name,
        "preferred_phone": preferred_phone,
        "preferred_email": preferred_email,
        "preferred_dob": preferred_dob,
        "preferred_address_id": preferred_address_id,
        "profile_completeness_score": profile_completeness_score,
        "golden_profile_version": _GOLDEN_PROFILE_VERSION,
    }

    logger.info(
        "Golden profile computed for person %s (completeness=%.2f)",
        person_id, profile_completeness_score,
    )
    return profile


# ------------------------------------------------------------------
# Survivorship helpers
# ------------------------------------------------------------------

def _pick_best_fact(
    facts: list[dict[str, Any]],
    attribute_name: str,
) -> str | None:
    """Pick the best attribute value using survivorship rules.

    Priority: valid quality > other, higher trust tier > lower, newer > older.
    """
    matching = [f for f in facts if f["attribute_name"] == attribute_name]
    if not matching:
        return None

    def _sort_key(f: dict[str, Any]) -> tuple:
        quality_score = 1 if f.get("quality_flag") == "valid" else 0
        tier_score = _TRUST_TIER_RANK.get(f.get("source_trust_tier", ""), 0)
        observed = f.get("observed_at") or ""
        return (quality_score, tier_score, str(observed))

    matching.sort(key=_sort_key, reverse=True)
    return matching[0]["attribute_value"]


def _pick_best_identifier(
    identifiers: list[dict[str, Any]],
    identifier_type: str,
) -> str | None:
    """Pick the best identifier value: verified > unverified, newer > older."""
    matching = [
        i for i in identifiers
        if i["identifier_type"] == identifier_type
    ]
    if not matching:
        return None

    def _sort_key(i: dict[str, Any]) -> tuple:
        verified_score = 1 if i.get("is_verified") else 0
        confirmed = i.get("last_confirmed_at") or ""
        return (verified_score, str(confirmed))

    matching.sort(key=_sort_key, reverse=True)
    return matching[0]["normalized_value"]


def _pick_best_address(addresses: list[dict[str, Any]]) -> str | None:
    """Pick the best address: verified > unverified, most recently confirmed."""
    if not addresses:
        return None

    def _sort_key(a: dict[str, Any]) -> tuple:
        verified_score = 1 if a.get("is_verified") else 0
        confirmed = a.get("last_confirmed_at") or ""
        return (verified_score, str(confirmed))

    addresses.sort(key=_sort_key, reverse=True)
    return addresses[0]["address_id"]
