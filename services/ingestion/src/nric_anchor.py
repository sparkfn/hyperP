"""NRIC anchor gate, conflict semantics, and link decisions.

This module provides the policy gate deciding whether an NRIC identifier on an
incoming record is permitted to promote a new Person or anchor an identity, along
with conflict classification across multiple NRIC values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.graph.bootstrap import MATCH_ONLY_SOURCE_KEYS, SOURCE_NRIC_TRUST
from src.models import NormalizedIdentifier, RecordType
from src.normalizers.name import normalize_name
from src.normalizers.nric import is_nric_digest

_TIERS_WITHIN_FLOOR: frozenset[str] = frozenset({"tier_1", "tier_2", "tier_3", "tier_4"})

NricGateReason = Literal[
    "anchor",
    "not_nric",
    "no_key",
    "checksum_invalid",
    "source_not_promoting",
    "below_tier_floor",
    "conversation_record",
    "nric_contradiction",
]

LinkDecisionKind = Literal["alias", "split"]


@dataclass(frozen=True)
class NameDob:
    """Identity name and date-of-birth pair for contradiction checking."""

    full_name: str | None = None
    dob: str | None = None


@dataclass(frozen=True)
class NricGateResult:
    """Outcome of evaluating the NRIC anchor gate."""

    promotes_person: bool
    reason: NricGateReason


@dataclass(frozen=True)
class LinkDecision:
    """A link-level decision (e.g. alias across multiple valid NRICs)."""

    kind: LinkDecisionKind
    person_id: str
    nric_keys: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class NricValue:
    """An NRIC value (digest) and its checksum validity flag."""

    digest: str
    checksum_valid: bool | None


# rebase(#490): use identity_registry.source_promotes_person
def _source_promotes_person(source_key: str) -> bool:
    """Return True if source_key promotes a person (seeded and not match_only)."""
    if source_key in MATCH_ONLY_SOURCE_KEYS:
        return False
    return source_key in SOURCE_NRIC_TRUST


def nric_anchor_gate(
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
    holder: NameDob | None,
) -> NricGateResult:
    """Evaluate the 7 ordered checks determining if an NRIC can anchor/promote.

    First check that fails returns ``promotes_person=False`` with that reason.
    If all checks pass, returns ``promotes_person=True`` with reason ``'anchor'``.
    """
    # 1. Identifier type must be nric
    # rebase(#490): and identifier_policy("nric").promotes
    if identifier.identifier_type != "nric":
        return NricGateResult(promotes_person=False, reason="not_nric")

    # 2. Normalized value must be digest-shaped (keyed)
    if not is_nric_digest(identifier.normalized_value):
        return NricGateResult(promotes_person=False, reason="no_key")

    # 3. Checksum must be valid
    if identifier.checksum_valid is not True:
        return NricGateResult(promotes_person=False, reason="checksum_invalid")

    # 4. Source must promote a person
    # rebase(#490): use identity_registry.source_promotes_person
    if not _source_promotes_person(source_key):
        return NricGateResult(promotes_person=False, reason="source_not_promoting")

    # 5. Source NRIC trust tier must be at or above floor tier_4
    if SOURCE_NRIC_TRUST.get(source_key) not in _TIERS_WITHIN_FLOOR:
        return NricGateResult(promotes_person=False, reason="below_tier_floor")

    # 6. Record type must not be conversation
    if record_type == RecordType.CONVERSATION:
        return NricGateResult(promotes_person=False, reason="conversation_record")

    # 7. No contradiction with existing holder
    if holder is not None:
        # Check DOB contradiction: both present and different
        if incoming.dob and holder.dob and incoming.dob != holder.dob:
            return NricGateResult(promotes_person=False, reason="nric_contradiction")

        # Check Name contradiction: both present and normalized names different
        if incoming.full_name and holder.full_name:
            norm_inc, _ = normalize_name(incoming.full_name)
            norm_hol, _ = normalize_name(holder.full_name)
            if norm_inc and norm_hol and norm_inc != norm_hol:
                return NricGateResult(promotes_person=False, reason="nric_contradiction")

    return NricGateResult(promotes_person=True, reason="anchor")


def classify_person_nrics(
    values: Sequence[NricValue],
) -> Literal["none", "single", "alias", "invalid_only"]:
    """Classify the set of NRIC values associated with a Person.

    - none: no values
    - deduplicated by digest:
      - >1 distinct valid digests -> alias
      - 1 distinct valid digest -> single
      - >=1 values, none valid -> invalid_only
    """
    if not values:
        return "none"

    # Deduplicate by digest, keeping best checksum_valid
    deduped: dict[str, bool | None] = {}
    for v in values:
        existing = deduped.get(v.digest)
        if existing is True:
            continue
        deduped[v.digest] = v.checksum_valid

    valid_digests = [d for d, valid in deduped.items() if valid is True]
    if len(valid_digests) > 1:
        return "alias"
    if len(valid_digests) == 1:
        return "single"
    return "invalid_only"


def alias_link_decision(
    person_id: str,
    values: Sequence[NricValue],
) -> LinkDecision | None:
    """Produce an alias LinkDecision if a Person has multiple distinct valid NRICs."""
    classification = classify_person_nrics(values)
    if classification != "alias":
        return None

    valid_digests = sorted({v.digest for v in values if v.checksum_valid is True})
    return LinkDecision(
        kind="alias",
        person_id=person_id,
        nric_keys=tuple(valid_digests),
        reason="Multiple distinct valid government IDs attached to person",
    )
