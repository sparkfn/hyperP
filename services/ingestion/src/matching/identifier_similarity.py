"""Approximate (near-miss) matching for phone and email identifiers.

Pure functions using ``phonenumbers`` and the shared similarity helpers, whose OSA
primitive is RapidFuzz-backed. This is a *weak*, corroborating-only second-pass signal (see
matching-spec "Approximate Identifier Matching"): never used for candidate
generation or deterministic rules, and never sufficient alone to cross the
review threshold.
"""

from __future__ import annotations

from functools import lru_cache

import phonenumbers

from src.matching.similarity import damerau_levenshtein_distance, jaro_winkler_similarity

#: Same-region NSN edit-distance at/below which two phone numbers are a near-match.
PHONE_NSN_EDIT_DISTANCE_THRESHOLD = 1

#: Domain edit-distance at/below which two email domains are a near-match
#: (only considered when the local parts are byte-identical).
EMAIL_DOMAIN_EDIT_DISTANCE_THRESHOLD = 1

#: Jaro-Winkler similarity at/above which two email local parts are a
#: near-match (only considered when the domains are identical).
EMAIL_LOCAL_PART_JW_THRESHOLD = 0.90

#: Local parts shorter than this are never compared on the local-part axis —
#: Jaro-Winkler similarity is unreliable for very short strings.
EMAIL_LOCAL_PART_MIN_LENGTH = 4

#: Domains that anchor the email domain-typo axis — derived from the top
#: domains observed in source dumps (see design doc background). Apple
#: relay and internal-staff domains are deliberately excluded.
EMAIL_KNOWN_DOMAINS = frozenset(
    {
        "gmail.com",
        "hotmail.com",
        "hotmail.sg",
        "yahoo.com",
        "yahoo.com.sg",
        "outlook.com",
        "icloud.com",
        "live.com",
    }
)

#: Region code phonenumbers assigns to non-geographic (e.g. satellite)
#: numbering plans — never treated as a shared region between two numbers.
_NON_GEOGRAPHIC_REGION = "001"


@lru_cache(maxsize=65_536)
def _region_and_nsn(value: str) -> tuple[str, str] | None:
    """Return ``(region_code, national_significant_number)`` for an E.164 value.

    Returns ``None`` when the value cannot be parsed, has no associated
    region (e.g. non-geographic numbers), or is in a non-geographic numbering
    plan (region "001", e.g. satellite prefixes).
    """
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(parsed)
    if region is None or region == _NON_GEOGRAPHIC_REGION:
        return None
    return region, phonenumbers.national_significant_number(parsed)


def phone_near_match(value1: str, value2: str) -> bool:
    """True if two E.164 phone numbers are a same-region single-digit-edit near-miss.

    Both inputs must already be normalized E.164 strings. Cross-region pairs
    are never near-matches — this is the direct mitigation for the
    region-ambiguity bug described in the design doc background (Track A
    fixes normalization; this gate stops any residual ambiguity from
    producing a cross-country near-match).
    """
    parsed1 = _region_and_nsn(value1)
    parsed2 = _region_and_nsn(value2)
    if parsed1 is None or parsed2 is None:
        return False
    region1, nsn1 = parsed1
    region2, nsn2 = parsed2
    if region1 != region2:
        return False
    return damerau_levenshtein_distance(nsn1, nsn2) == PHONE_NSN_EDIT_DISTANCE_THRESHOLD


@lru_cache(maxsize=65_536)
def _split_email(value: str) -> tuple[str, str] | None:
    local, sep, domain = value.rpartition("@")
    if not sep or not local or not domain:
        return None
    return local, domain


def email_near_match(value1: str, value2: str) -> bool:
    """True if two normalized emails differ on exactly one fuzzy axis.

    Either the local parts are identical and the domains differ by a single
    edit (with at least one domain in :data:`EMAIL_KNOWN_DOMAINS`), or the
    domains are identical and the local parts are a close Jaro-Winkler match.
    Never both axes at once — keeps false-positive risk bounded.
    """
    parts1 = _split_email(value1)
    parts2 = _split_email(value2)
    if parts1 is None or parts2 is None:
        return False
    local1, domain1 = parts1
    local2, domain2 = parts2

    if local1 == local2 and domain1 != domain2:
        if domain1 not in EMAIL_KNOWN_DOMAINS and domain2 not in EMAIL_KNOWN_DOMAINS:
            return False
        return (
            damerau_levenshtein_distance(domain1, domain2) == EMAIL_DOMAIN_EDIT_DISTANCE_THRESHOLD
        )

    if domain1 == domain2 and local1 != local2:
        if len(local1) < EMAIL_LOCAL_PART_MIN_LENGTH or len(local2) < EMAIL_LOCAL_PART_MIN_LENGTH:
            return False
        return jaro_winkler_similarity(local1, local2) >= EMAIL_LOCAL_PART_JW_THRESHOLD

    return False
