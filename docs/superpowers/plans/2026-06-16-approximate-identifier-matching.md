# Approximate Identifier Matching (Phone & Email) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add normalization-correctness fixes (region-hint phone parsing, Gmail/Googlemail canonicalization) and a new weak, corroborating-only "near-miss" phone/email signal to the Layer 2 heuristic scorer, per `docs/superpowers/specs/2026-06-16-approximate-identifier-matching-design.md`.

**Architecture:** Track A (normalization correctness) fixes the *exact*-match normalized values so true equivalences (Gmail dot/plus variants) and region-correct E.164 phone numbers land in the same Identifier node. Track B adds a new pure-function module `matching/identifier_similarity.py` (phone digit-typo / email near-miss detection) and wires it into `matching/heuristic.py` as a second scoring pass that sets new `phone_approx_match`/`email_approx_match` signals — fields the existing promotion logic never reads, so approximate evidence structurally cannot trigger auto-merge.

**Tech Stack:** Python 3.13, `phonenumbers`, pydantic v2, pytest, ruff, mypy --strict (services/ingestion).

---

### Task 1: Damerau-Levenshtein distance helper

**Files:**
- Modify: `services/ingestion/src/matching/similarity.py`
- Test: `services/ingestion/tests/test_similarity.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_similarity.py`:

```python
"""Unit tests for stdlib string-similarity helpers."""

from __future__ import annotations

from src.matching.similarity import damerau_levenshtein_distance


def test_identical_strings_have_zero_distance() -> None:
    assert damerau_levenshtein_distance("96427694", "96427694") == 0


def test_single_substitution_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "96427699") == 1


def test_adjacent_transposition_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "96472694") == 1


def test_single_insertion_is_distance_one() -> None:
    assert damerau_levenshtein_distance("9642769", "96427694") == 1


def test_single_deletion_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "9642769") == 1


def test_two_substitutions_is_distance_two() -> None:
    assert damerau_levenshtein_distance("96427694", "96427799") == 2


def test_empty_strings() -> None:
    assert damerau_levenshtein_distance("", "") == 0
    assert damerau_levenshtein_distance("", "a") == 1
    assert damerau_levenshtein_distance("a", "") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_similarity.py -v`
Expected: FAIL with `ImportError: cannot import name 'damerau_levenshtein_distance'`

- [ ] **Step 3: Implement the helper**

Append to `services/ingestion/src/matching/similarity.py` (after `jaro_winkler_similarity`):

```python


def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Damerau-Levenshtein edit distance (optimal string alignment).

    Counts single-character insertions, deletions, substitutions, and adjacent
    transpositions as one edit each. This is the "restricted"/optimal-string-
    alignment variant (a transposed pair cannot share characters with another
    edit), which is sufficient for the single-edit near-match checks in
    ``matching.identifier_similarity``.
    """
    len1, len2 = len(s1), len(s2)
    distances = [[0] * (len2 + 1) for _ in range(len1 + 1)]

    for i in range(len1 + 1):
        distances[i][0] = i
    for j in range(len2 + 1):
        distances[0][j] = j

    for i in range(1, len1 + 1):
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            distances[i][j] = min(
                distances[i - 1][j] + 1,  # deletion
                distances[i][j - 1] + 1,  # insertion
                distances[i - 1][j - 1] + cost,  # substitution
            )
            if i > 1 and j > 1 and s1[i - 1] == s2[j - 2] and s1[i - 2] == s2[j - 1]:
                distances[i][j] = min(distances[i][j], distances[i - 2][j - 2] + 1)

    return distances[len1][len2]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_similarity.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/matching/similarity.py services/ingestion/tests/test_similarity.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/matching/similarity.py services/ingestion/tests/test_similarity.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/matching/similarity.py
```
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/matching/similarity.py services/ingestion/tests/test_similarity.py
git commit -m "feat(ingestion): add Damerau-Levenshtein distance helper"
```

---

### Task 2: Phone/email approximate-match helpers (Track B1/B2/B3)

**Files:**
- Create: `services/ingestion/src/matching/identifier_similarity.py`
- Test: `services/ingestion/tests/test_identifier_similarity.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_identifier_similarity.py`:

```python
"""Phone/email near-match (approximate identifier) scoring helpers."""

from __future__ import annotations

from src.matching.identifier_similarity import email_near_match, phone_near_match


# --- phone_near_match -------------------------------------------------------


def test_phone_single_digit_substitution_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+6591234568") is True


def test_phone_adjacent_digit_transposition_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+6591234657") is True


def test_phone_single_digit_insertion_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+65912345670") is True


def test_phone_single_digit_deletion_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+659123456") is True


def test_phone_two_digit_difference_is_not_near_match() -> None:
    assert phone_near_match("+6591234567", "+6591234599") is False


def test_phone_identical_numbers_are_not_near_match() -> None:
    # Exact equality is handled by the exact-match pass, not this function.
    assert phone_near_match("+6591234567", "+6591234567") is False


def test_phone_cross_region_one_digit_difference_is_not_near_match() -> None:
    # Same NSN-edit-distance-1, but Hong Kong (+852) vs Singapore (+65) — the
    # region gate blocks it regardless of digit distance.
    assert phone_near_match("+6591234567", "+85291234568") is False


def test_phone_invalid_value_is_not_near_match() -> None:
    assert phone_near_match("not-a-phone", "+6591234567") is False


# --- email_near_match --------------------------------------------------------


def test_email_known_domain_typo_with_exact_local_is_near_match() -> None:
    assert email_near_match("john@gmial.com", "john@gmail.com") is True


def test_email_local_part_near_miss_with_same_domain_is_near_match() -> None:
    assert email_near_match("johnsmith@gmail.com", "jonhsmith@gmail.com") is True


def test_email_both_axes_fuzzy_is_not_near_match() -> None:
    assert email_near_match("johnsmith@gmial.com", "jonhsmith@gmail.com") is False


def test_email_unknown_domain_typo_is_not_near_match() -> None:
    assert email_near_match("john@foo.com", "john@fooo.com") is False


def test_email_short_local_part_is_not_near_match() -> None:
    assert email_near_match("abc@gmail.com", "abd@gmail.com") is False


def test_email_identical_addresses_are_not_near_match() -> None:
    assert email_near_match("john@gmail.com", "john@gmail.com") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_identifier_similarity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.matching.identifier_similarity'`

- [ ] **Step 3: Implement the module**

Create `services/ingestion/src/matching/identifier_similarity.py`:

```python
"""Approximate (near-miss) matching for phone and email identifiers.

Pure functions, stdlib + ``phonenumbers`` only — parallel to ``matching.names``.
This is a *weak*, corroborating-only second-pass heuristic signal (see
matching-spec "Approximate Identifier Matching"): never used for candidate
generation or deterministic rules, and never sufficient alone to cross the
review threshold.
"""

from __future__ import annotations

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


def _region_and_nsn(value: str) -> tuple[str, str] | None:
    """Return ``(region_code, national_significant_number)`` for an E.164 value.

    Returns ``None`` when the value cannot be parsed or has no associated
    region (e.g. non-geographic numbers).
    """
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(parsed)
    if region is None:
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
        if (
            len(local1) < EMAIL_LOCAL_PART_MIN_LENGTH
            or len(local2) < EMAIL_LOCAL_PART_MIN_LENGTH
        ):
            return False
        return jaro_winkler_similarity(local1, local2) >= EMAIL_LOCAL_PART_JW_THRESHOLD

    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_identifier_similarity.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/matching/identifier_similarity.py services/ingestion/tests/test_identifier_similarity.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/matching/identifier_similarity.py services/ingestion/tests/test_identifier_similarity.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/matching/identifier_similarity.py
```
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/matching/identifier_similarity.py services/ingestion/tests/test_identifier_similarity.py
git commit -m "feat(ingestion): add phone/email approximate near-match helpers (Track B)"
```

---

### Task 3: Gmail/Googlemail canonicalization (Track A2)

**Files:**
- Modify: `services/ingestion/src/normalizers/email.py`
- Test: `services/ingestion/tests/test_email_normalizer.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_email_normalizer.py`:

```python
"""Email normalization, including Gmail dot/plus-addressing canonicalization."""

from __future__ import annotations

from src.models import QualityFlag
from src.normalizers.email import normalize_email


def test_lowercases_and_strips_whitespace() -> None:
    assert normalize_email("  John.Tan@Example.com  ") == ("john.tan@example.com", QualityFlag.VALID)


def test_gmail_dot_in_local_part_is_removed() -> None:
    assert normalize_email("john.tan@gmail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_gmail_plus_tag_is_stripped() -> None:
    assert normalize_email("johntan+promo@gmail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_gmail_dot_and_plus_tag_combined() -> None:
    assert normalize_email("John.Tan+promo@Gmail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_googlemail_domain_folds_to_gmail() -> None:
    assert normalize_email("john.tan@googlemail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_non_gmail_domain_is_unaffected() -> None:
    assert normalize_email("john.tan+promo@hotmail.com") == (
        "john.tan+promo@hotmail.com",
        QualityFlag.VALID,
    )


def test_placeholder_still_rejected() -> None:
    assert normalize_email("test@test.com") == (None, QualityFlag.PLACEHOLDER_VALUE)


def test_invalid_format_still_rejected() -> None:
    assert normalize_email("not-an-email") == (None, QualityFlag.INVALID_FORMAT)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_email_normalizer.py -v`
Expected: FAIL — the Gmail-canonicalization tests assert `johntan@gmail.com` but current code returns the raw lowercased value (e.g. `john.tan@gmail.com`).

- [ ] **Step 3: Implement Gmail/Googlemail canonicalization**

Replace the body of `services/ingestion/src/normalizers/email.py` from `def normalize_email` onward:

```python
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def _canonicalize_gmail(local: str, domain: str) -> tuple[str, str]:
    """Apply Gmail's dot-insensitive, plus-addressing equivalence.

    ``googlemail.com`` is Gmail's legacy domain — both map to ``gmail.com``.
    A ``+tag`` suffix and any ``.`` in the local part are routing artifacts;
    Gmail delivers all variants to the same mailbox.
    """
    if domain not in _GMAIL_DOMAINS:
        return local, domain
    canonical_local = local.split("+", 1)[0].replace(".", "")
    if not canonical_local:
        return local, domain
    return canonical_local, "gmail.com"


def normalize_email(raw: str) -> tuple[str | None, QualityFlag]:
    """Return ``(normalized_email, quality_flag)`` for a raw email string.

    Normalization: lowercase, strip whitespace, canonicalize Gmail/Googlemail
    addresses (dot-insensitive local part, ``+tag`` stripped, domain folded to
    ``gmail.com``).
    """
    stripped = raw.strip().lower()
    if not stripped:
        return None, QualityFlag.INVALID_FORMAT

    if stripped in _PLACEHOLDER_PATTERNS:
        return None, QualityFlag.PLACEHOLDER_VALUE

    if not _EMAIL_RE.match(stripped):
        return None, QualityFlag.INVALID_FORMAT

    local, _, domain = stripped.rpartition("@")
    local, domain = _canonicalize_gmail(local, domain)
    return f"{local}@{domain}", QualityFlag.VALID
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_email_normalizer.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the full normalizer test suite (regression check)**

Run: `uv run pytest services/ingestion/tests -k email -v`
Expected: PASS — no existing email-related test breaks.

- [ ] **Step 6: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/normalizers/email.py services/ingestion/tests/test_email_normalizer.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/normalizers/email.py services/ingestion/tests/test_email_normalizer.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/normalizers/email.py
```
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add services/ingestion/src/normalizers/email.py services/ingestion/tests/test_email_normalizer.py
git commit -m "feat(ingestion): canonicalize Gmail/Googlemail addresses (Track A2)"
```

---

### Task 4: Region-hint phone normalization fallback chain (Track A1 core)

**Files:**
- Modify: `services/ingestion/src/models.py:98-103` (`RawIdentifier`)
- Modify: `services/ingestion/src/pipeline_normalization.py`
- Test: `services/ingestion/tests/test_pipeline_normalization.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_pipeline_normalization.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_pipeline_normalization.py -v`
Expected: FAIL — `RawIdentifier(type="phone", value="96542555", region_hint="MY")` raises a pydantic validation error (`region_hint` is an unknown field... actually pydantic v2 default `extra="ignore"` would silently drop it, so the MY-hint test fails on the assertion `== "+6096542555"` instead, getting `+6596542555`).

- [ ] **Step 3: Add `region_hint` to `RawIdentifier`**

In `services/ingestion/src/models.py`, modify the `RawIdentifier` class (around line 98-103):

```python
class RawIdentifier(BaseModel):
    """A single identifier as it arrives from the source system."""

    type: str
    value: str
    is_verified: bool = False
    region_hint: str | None = None
```

- [ ] **Step 4: Implement the fallback chain in `pipeline_normalization.py`**

Add a new helper after `_passthrough_normalize` in `services/ingestion/src/pipeline_normalization.py`:

```python
def _normalize_phone_with_hint(
    value: str, region_hint: str | None
) -> tuple[str | None, QualityFlag]:
    """Normalize a phone number, preferring a connector-supplied region hint.

    Falls back to :func:`normalize_phone`'s default region (SG) when the
    hinted region fails to produce a usable number — a noisy or wrong
    ``country``/``phone_code`` hint can therefore never make a number that
    normalizes fine today start failing.
    """
    if region_hint is None:
        return normalize_phone(value)
    hinted = normalize_phone(value, region=region_hint)
    if hinted[1] != QualityFlag.INVALID_FORMAT:
        return hinted
    return normalize_phone(value)
```

Then update `normalize_envelope_identifiers` to special-case `phone`:

```python
def normalize_envelope_identifiers(
    envelope: SourceRecordEnvelope,
) -> list[NormalizedIdentifier]:
    results: list[NormalizedIdentifier] = []
    for raw_id in envelope.identifiers:
        id_type = raw_id.type.lower().strip()
        if id_type == "phone":
            normalized, flag = _normalize_phone_with_hint(raw_id.value, raw_id.region_hint)
        else:
            normalizer = _IDENTIFIER_NORMALIZERS.get(id_type, _passthrough_normalize)
            normalized, flag = normalizer(raw_id.value)
        if normalized:
            results.append(
                NormalizedIdentifier(
                    identifier_type=id_type,
                    normalized_value=normalized,
                    is_verified=raw_id.is_verified,
                    quality_flag=flag,
                )
            )
        else:
            logger.warning(
                "%s normalization failed for %s: %s",
                id_type,
                raw_id.value,
                flag,
            )
    return results
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_pipeline_normalization.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full models + pipeline test suite (regression check)**

Run: `uv run pytest services/ingestion/tests -k "model or pipeline or normaliz" -v`
Expected: PASS

- [ ] **Step 7: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/models.py services/ingestion/src/pipeline_normalization.py services/ingestion/tests/test_pipeline_normalization.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/models.py services/ingestion/src/pipeline_normalization.py services/ingestion/tests/test_pipeline_normalization.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/models.py services/ingestion/src/pipeline_normalization.py
```
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add services/ingestion/src/models.py services/ingestion/src/pipeline_normalization.py services/ingestion/tests/test_pipeline_normalization.py
git commit -m "feat(ingestion): region-hint phone normalization fallback chain (Track A1)"
```

---

### Task 5: `phone_region_hint()` helper + `IdentifierBag.add(region_hint=...)`

**Files:**
- Modify: `services/ingestion/src/connectors/fundbox/builders.py`
- Test: `services/ingestion/tests/test_phone_region_hint.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_phone_region_hint.py`:

```python
"""phone_region_hint() — region derivation from POS country/phone_code columns."""

from __future__ import annotations

from src.connectors.fundbox.builders import IdentifierBag, phone_region_hint


def test_phone_code_takes_precedence() -> None:
    assert phone_region_hint("60", "Singapore") == "MY"


def test_phone_code_singapore() -> None:
    assert phone_region_hint("65", "SG") == "SG"


def test_falls_back_to_country_text_when_no_phone_code() -> None:
    assert phone_region_hint(None, "Malaysia") == "MY"


def test_country_text_is_case_and_whitespace_insensitive() -> None:
    assert phone_region_hint(None, " SIngapore ") == "SG"


def test_unrecognized_country_returns_none() -> None:
    assert phone_region_hint(None, "Atlantis") is None


def test_non_numeric_phone_code_falls_back_to_country() -> None:
    assert phone_region_hint("unknown", "Malaysia") == "MY"


def test_both_absent_returns_none() -> None:
    assert phone_region_hint(None, None) is None


def test_identifier_bag_carries_region_hint() -> None:
    bag = IdentifierBag()
    bag.add("phone", "123456789", region_hint="MY")
    bag.add("email", "ada@example.com")
    assert bag.items[0] == {
        "type": "phone",
        "value": "123456789",
        "is_verified": False,
        "region_hint": "MY",
    }
    assert "region_hint" not in bag.items[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_phone_region_hint.py -v`
Expected: FAIL with `ImportError: cannot import name 'phone_region_hint'`

- [ ] **Step 3: Implement `phone_region_hint()` and extend `IdentifierBag.add`**

In `services/ingestion/src/connectors/fundbox/builders.py`, add the import and module-level constants/function near the top (after the existing imports, around line 15):

```python
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal

import phonenumbers

from src.connectors.fundbox.junk import is_junk_identifier, should_filter
from src.models import JsonValue


_COUNTRY_NAME_TO_REGION: dict[str, str] = {
    "singapore": "SG",
    "sg": "SG",
    "malaysia": "MY",
    "malaysian": "MY",
    "indonesia": "ID",
    "indonesian": "ID",
    "philippines": "PH",
    "filipino": "PH",
}


def phone_region_hint(phone_code: object, country: object) -> str | None:
    """Derive an ISO region hint for phone normalization from POS columns.

    Prefers the numeric calling code (``phone_code``, e.g. ``"60"``); falls
    back to a free-text ``country`` column. Returns ``None`` when neither
    yields a recognized region, so the normalizer falls back to its default
    (SG).
    """
    if phone_code is not None:
        code_str = str(phone_code).strip()
        if code_str.isdigit():
            region = phonenumbers.region_code_for_country_code(int(code_str))
            if region and region != "ZZ":
                return region
    if country is not None:
        country_key = str(country).strip().lower()
        if country_key in _COUNTRY_NAME_TO_REGION:
            return _COUNTRY_NAME_TO_REGION[country_key]
    return None
```

Then modify `IdentifierBag.add` (around line 150-176) to accept and carry `region_hint`:

```python
    def add(
        self,
        id_type: str,
        value: object,
        *,
        verified: bool = False,
        last_confirmed_at: str | None = None,
        region_hint: str | None = None,
    ) -> None:
        if value is None:
            return
        value_str = str(value).strip()
        if not value_str:
            return
        if should_filter(id_type) and is_junk_identifier(value_str):
            return
        key = (id_type, value_str)
        if key in self._seen:
            return
        self._seen.add(key)
        item: dict[str, JsonValue] = {
            "type": id_type,
            "value": value_str,
            "is_verified": verified,
        }
        if last_confirmed_at is not None:
            item["last_confirmed_at"] = last_confirmed_at
        if region_hint is not None:
            item["region_hint"] = region_hint
        self.items.append(item)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_phone_region_hint.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run the fundbox builders test suite (regression check)**

Run: `uv run pytest services/ingestion/tests -k builders -v`
Expected: PASS

- [ ] **Step 6: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/connectors/fundbox/builders.py services/ingestion/tests/test_phone_region_hint.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/connectors/fundbox/builders.py services/ingestion/tests/test_phone_region_hint.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/connectors/fundbox/builders.py
```
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add services/ingestion/src/connectors/fundbox/builders.py services/ingestion/tests/test_phone_region_hint.py
git commit -m "feat(ingestion): derive phone region hints from POS country/phone_code columns"
```

---

### Task 6: Wire region hints into the eko connector

**Files:**
- Modify: `services/ingestion/src/connectors/eko/connector.py`
- Test: `services/ingestion/tests/test_dump_connectors.py` (new test function appended)

- [ ] **Step 1: Write the failing test**

Append to `services/ingestion/tests/test_dump_connectors.py` (after `test_eko_dump_connector_yields_identity_envelope`):

```python
def test_eko_dump_connector_derives_phone_region_hint(tmp_path: Path) -> None:
    dump_path = tmp_path / "eko_my.sql"
    dump_path.write_text(
        """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `address_1` varchar(255),
  `address_2` varchar(255),
  `city` varchar(255),
  `state` varchar(255),
  `zip` varchar(255),
  `country` varchar(255),
  `comments` text,
  `create_date` datetime,
  `last_modified` datetime,
  `title` varchar(255),
  `phone_code` varchar(255)
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255),
  `custom_field_2_value` varchar(255),
  `custom_field_3_value` varchar(255),
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_6_value` varchar(255),
  `custom_field_7_value` varchar(255),
  `custom_field_8_value` varchar(255),
  `custom_field_9_value` varchar(255),
  `custom_field_10_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(9,'Wei','Tan','Wei Tan','96542555','wei@example.test','One','Two',
'Kuala Lumpur','KL','50000','Malaysia','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Mr','60');
INSERT INTO `phppos_customers` VALUES
(13,9,0,'ACC-13','Wei Co','S1234568A','unused-2','unused-3','2026-12-31','15',
'unused-6','unused-7','KL','1991-02-02','Y');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("eko_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    phone_items = [item for item in records[0]["identifiers"] if item["type"] == "phone"]
    assert phone_items == [
        {"type": "phone", "value": "96542555", "is_verified": False, "region_hint": "MY"}
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_dump_connectors.py::test_eko_dump_connector_derives_phone_region_hint -v`
Expected: FAIL — `phone_items == [{"type": "phone", "value": "96542555", "is_verified": False}]` (no `region_hint` key)

- [ ] **Step 3: Wire `phone_region_hint()` into both eko builders**

In `services/ingestion/src/connectors/eko/connector.py`, add `phone_region_hint` to the import from `src.connectors.fundbox.builders` (around line 25-32):

```python
from src.connectors.fundbox.builders import (
    IdentifierBag,
    address_from_row,
    build_envelope,
    format_address,
    phone_region_hint,
    serialize_row,
    to_iso,
)
```

In `_build_records_people_only` (around line 125-127), change:

```python
            ids = IdentifierBag()
            ids.add("email", row.email)
            ids.add("phone", row.phone_number)
```

to:

```python
            ids = IdentifierBag()
            ids.add("email", row.email)
            ids.add(
                "phone",
                row.phone_number,
                region_hint=phone_region_hint(row.phone_code, row.country),
            )
```

In `_build_one` (around line 188-191), change:

```python
        ids = IdentifierBag()
        ids.add("nric", row.custom_field_1_value, verified=True)
        ids.add("email", row.email)
        ids.add("phone", row.phone_number)
```

to:

```python
        ids = IdentifierBag()
        ids.add("nric", row.custom_field_1_value, verified=True)
        ids.add("email", row.email)
        ids.add(
            "phone",
            row.phone_number,
            region_hint=phone_region_hint(row.phone_code, row.country),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_dump_connectors.py -v -k eko`
Expected: PASS — both `test_eko_dump_connector_yields_identity_envelope` (unaffected: `country='SG'`/`phone_code='65'` → `phone_region_hint` returns `"SG"`, but that test only checks `phone_values`, not the full dict, so it's unaffected) and the new `test_eko_dump_connector_derives_phone_region_hint`.

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/connectors/eko/connector.py services/ingestion/tests/test_dump_connectors.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/connectors/eko/connector.py services/ingestion/tests/test_dump_connectors.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/connectors/eko/connector.py
```
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/connectors/eko/connector.py services/ingestion/tests/test_dump_connectors.py
git commit -m "feat(ingestion): wire phone region hints into eko connector"
```

---

### Task 7: Wire region hints into the speedzone connector

**Files:**
- Modify: `services/ingestion/src/connectors/speedzone/connector.py`
- Test: `services/ingestion/tests/test_dump_connectors.py` (new test function appended)

- [ ] **Step 1: Write the failing test**

Append to `services/ingestion/tests/test_dump_connectors.py` (after `test_speedzone_dump_connector_preserves_custom_field_mapping`):

```python
def test_speedzone_dump_connector_derives_phone_region_hint(tmp_path: Path) -> None:
    dump_path = tmp_path / "speedzone_my.sql"
    dump_path.write_text(
        """
CREATE TABLE `phppos_people` (
  `person_id` int NOT NULL,
  `first_name` varchar(255),
  `last_name` varchar(255),
  `full_name` varchar(255),
  `phone_number` varchar(255),
  `email` varchar(255),
  `address_1` varchar(255),
  `address_2` varchar(255),
  `city` varchar(255),
  `state` varchar(255),
  `zip` varchar(255),
  `country` varchar(255),
  `comments` text,
  `create_date` datetime,
  `last_modified` datetime,
  `title` varchar(255),
  `phone_code` varchar(255)
);
CREATE TABLE `phppos_customers` (
  `id` int NOT NULL,
  `person_id` int,
  `deleted` int,
  `account_number` varchar(255),
  `company_name` varchar(255),
  `custom_field_1_value` varchar(255),
  `custom_field_2_value` varchar(255),
  `custom_field_3_value` varchar(255),
  `custom_field_4_value` varchar(255),
  `custom_field_5_value` varchar(255),
  `custom_field_6_value` varchar(255),
  `custom_field_7_value` varchar(255),
  `custom_field_8_value` varchar(255),
  `custom_field_9_value` varchar(255),
  `custom_field_10_value` varchar(255)
);
INSERT INTO `phppos_people` VALUES
(10,'Wei','Tan','Wei Tan','96542555','wei@example.test','One','Two',
'Kuala Lumpur','KL','50000','Malaysia','notes','2026-05-01 01:00:00','2026-05-06 02:00:00',
'Mr','60');
INSERT INTO `phppos_customers` VALUES
(14,10,0,'ACC-14','Wei Co','S1234569A','unused-2','unused-3','2026-12-31','15',
'unused-6','unused-7','KL','1991-02-02','Y');
""".strip(),
        encoding="utf-8",
    )

    connector = get_dump_connector("speedzone_phppos", dump_path)
    records = list(connector.fetch_records())

    assert len(records) == 1
    phone_items = [item for item in records[0]["identifiers"] if item["type"] == "phone"]
    assert phone_items == [
        {"type": "phone", "value": "96542555", "is_verified": False, "region_hint": "MY"}
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_dump_connectors.py::test_speedzone_dump_connector_derives_phone_region_hint -v`
Expected: FAIL — `phone_items == [{"type": "phone", "value": "96542555", "is_verified": False}]` (no `region_hint` key)

- [ ] **Step 3: Wire `phone_region_hint()` into both speedzone builders**

In `services/ingestion/src/connectors/speedzone/connector.py`, add `phone_region_hint` to the import from `src.connectors.fundbox.builders` (around line 27-34):

```python
from src.connectors.fundbox.builders import (
    IdentifierBag,
    address_from_row,
    build_envelope,
    format_address,
    phone_region_hint,
    serialize_row,
    to_iso,
)
```

In `_build_envelope_with_customer` (around line 171-174), change:

```python
        ids = IdentifierBag()
        ids.add("nric", row.custom_field_1_value, verified=True)
        ids.add("email", row.email)
        ids.add("phone", row.phone_number)
```

to:

```python
        ids = IdentifierBag()
        ids.add("nric", row.custom_field_1_value, verified=True)
        ids.add("email", row.email)
        ids.add(
            "phone",
            row.phone_number,
            region_hint=phone_region_hint(row.phone_code, row.country),
        )
```

In `_build_envelope_people_only` (around line 200-202), change:

```python
        ids = IdentifierBag()
        ids.add("email", row.email)
        ids.add("phone", row.phone_number)
```

to:

```python
        ids = IdentifierBag()
        ids.add("email", row.email)
        ids.add(
            "phone",
            row.phone_number,
            region_hint=phone_region_hint(row.phone_code, row.country),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_dump_connectors.py -v -k speedzone`
Expected: PASS — both the existing `test_speedzone_dump_connector_preserves_custom_field_mapping` (only checks `phone_values`/identifier types, unaffected) and the new region-hint test.

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/connectors/speedzone/connector.py services/ingestion/tests/test_dump_connectors.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/connectors/speedzone/connector.py services/ingestion/tests/test_dump_connectors.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/connectors/speedzone/connector.py
```
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/connectors/speedzone/connector.py services/ingestion/tests/test_dump_connectors.py
git commit -m "feat(ingestion): wire phone region hints into speedzone connector"
```

---

### Task 8: `CandidateSnapshot.phones()` / `.emails()` list accessors

**Files:**
- Modify: `services/ingestion/src/matching/snapshot.py`
- Test: `services/ingestion/tests/test_candidate_snapshot.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_candidate_snapshot.py`:

```python
"""CandidateSnapshot list accessors used by approximate-match scoring."""

from __future__ import annotations

from src.matching.snapshot import CandidateSnapshot


def _snapshot() -> CandidateSnapshot:
    return CandidateSnapshot(
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6591234567", "is_verified": True},
            {"identifier_type": "phone", "normalized_value": "+6598765432", "is_verified": False},
            {"identifier_type": "email", "normalized_value": "ada@example.com", "is_verified": True},
            {"identifier_type": "nric", "normalized_value": "S1234567A", "is_verified": True},
        ],
        facts=[],
        addrs=[],
    )


def test_phones_returns_all_phone_records() -> None:
    snapshot = _snapshot()
    values = {str(r["normalized_value"]) for r in snapshot.phones()}
    assert values == {"+6591234567", "+6598765432"}


def test_emails_returns_all_email_records() -> None:
    snapshot = _snapshot()
    values = {str(r["normalized_value"]) for r in snapshot.emails()}
    assert values == {"ada@example.com"}


def test_phones_excludes_other_identifier_types() -> None:
    snapshot = _snapshot()
    assert all(r["identifier_type"] == "phone" for r in snapshot.phones())


def test_emails_is_empty_when_no_emails() -> None:
    snapshot = CandidateSnapshot(idents=[], facts=[], addrs=[])
    assert snapshot.emails() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_candidate_snapshot.py -v`
Expected: FAIL with `AttributeError: 'CandidateSnapshot' object has no attribute 'phones'`

- [ ] **Step 3: Implement `phones()` and `emails()`**

In `services/ingestion/src/matching/snapshot.py`, add two methods to `CandidateSnapshot` right after `emails_by_value()` (around line 62-63):

```python
    def phones(self) -> list[RecordDict]:
        """All candidate phone identifier records (for approximate matching)."""
        return [i for i in self.idents if i.get("identifier_type") == "phone"]

    def emails(self) -> list[RecordDict]:
        """All candidate email identifier records (for approximate matching)."""
        return [i for i in self.idents if i.get("identifier_type") == "email"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_candidate_snapshot.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/matching/snapshot.py services/ingestion/tests/test_candidate_snapshot.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/matching/snapshot.py services/ingestion/tests/test_candidate_snapshot.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/matching/snapshot.py
```
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add services/ingestion/src/matching/snapshot.py services/ingestion/tests/test_candidate_snapshot.py
git commit -m "feat(ingestion): add CandidateSnapshot phone/email list accessors"
```

---

### Task 9: Heuristic integration — approximate phone/email scoring (Layer 2)

**Files:**
- Modify: `services/ingestion/src/matching/heuristic.py`
- Test: `services/ingestion/tests/test_heuristic_approx_match.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_heuristic_approx_match.py`. This calls `evaluate_heuristic` directly (not via `MatchEngine`) so the full `feature_snapshot` is inspectable regardless of decision band:

```python
"""Approximate (near-miss) phone/email scoring — Track B heuristic integration."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.heuristic import evaluate_heuristic
from src.models import MatchDecision, NormalizedIdentifier, QualityFlag, RecordType


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _Tx:
    """Candidate has phone +6591234567 and email ada@gmail.com (both system-sourced)."""

    def run(self, query: str, **_params: object) -> _Result:
        if "[rel:IDENTIFIED_BY]->" in query:
            return _Result(
                [
                    {
                        "identifier_type": "phone",
                        "normalized_value": "+6591234567",
                        "is_verified": False,
                        "last_confirmed_at": None,
                        "source_record_type": "identity",
                    },
                    {
                        "identifier_type": "email",
                        "normalized_value": "ada@gmail.com",
                        "is_verified": False,
                        "last_confirmed_at": None,
                        "source_record_type": "identity",
                    },
                ]
            )
        if "[f:HAS_FACT]->" in query:
            return _Result([])
        if "[rel:LIVES_AT]->" in query:
            return _Result([])
        if "AS fanout" in query:
            return _Result([{"fanout": 1}])
        return _Result([])


def test_phone_near_miss_scores_small_positive_signal() -> None:
    # Incoming +6591234568 is a 1-digit typo of the candidate's +6591234567.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234568",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.IDENTITY,
    )

    assert result.feature_snapshot["phone_approx_match"] is True
    assert result.feature_snapshot["phone_exact_match"] is False
    assert result.decision == MatchDecision.NO_MATCH
    assert any("near-match" in r.lower() for r in result.reasons)


def test_email_near_miss_scores_small_positive_signal() -> None:
    # Incoming ada@gmial.com is a known-domain typo of the candidate's ada@gmail.com.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="email",
                normalized_value="ada@gmial.com",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.IDENTITY,
    )

    assert result.feature_snapshot["email_approx_match"] is True
    assert result.feature_snapshot["email_exact_match"] is False
    assert result.decision == MatchDecision.NO_MATCH


def test_conversation_record_with_only_approx_phone_does_not_promote() -> None:
    # Approximate evidence alone must never enable conversation promotion —
    # phone_exact_match stays False, so _can_promote_conversation's
    # has_identifier check fails regardless of the approx signal.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234568",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.CONVERSATION,
    )

    assert result.decision != MatchDecision.MERGE
    assert result.feature_snapshot["conversation_promotion"] is False


def test_exact_match_takes_precedence_over_approx() -> None:
    # When the incoming phone exactly matches, no approximate scoring runs for
    # that identifier — phone_approx_match stays False.
    result = evaluate_heuristic(
        _Tx(),  # type: ignore[arg-type]
        "person-1",
        [
            NormalizedIdentifier(
                identifier_type="phone",
                normalized_value="+6591234567",
                is_verified=False,
                quality_flag=QualityFlag.VALID,
            )
        ],
        None,
        [],
        record_type=RecordType.IDENTITY,
    )

    assert result.feature_snapshot["phone_exact_match"] is True
    assert result.feature_snapshot["phone_approx_match"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_heuristic_approx_match.py -v`
Expected: FAIL with `KeyError: 'phone_approx_match'` (key not yet in `feature_snapshot`)

- [ ] **Step 3: Add imports and new constants**

In `services/ingestion/src/matching/heuristic.py`, update the imports (lines 17-32):

```python
from src.matching.identifier_similarity import email_near_match, phone_near_match
from src.matching.names import (
    NAME_PARTIAL_THRESHOLD,
    best_name_similarity,
    incoming_names,
)
from src.matching.snapshot import CandidateSnapshot, RecordDict, fetch_candidate_snapshot
```

Add two new constants after `EMAIL_UNVERIFIED_WEIGHT = 0.20` (line 45):

```python
EMAIL_UNVERIFIED_WEIGHT = 0.20
PHONE_APPROX_WEIGHT = 0.10  # half of PHONE_UNVERIFIED_WEIGHT — weak corroborating signal
EMAIL_APPROX_WEIGHT = 0.10  # half of EMAIL_UNVERIFIED_WEIGHT — weak corroborating signal
```

- [ ] **Step 4: Add new `HeuristicSignals` fields**

In the `HeuristicSignals` dataclass (lines 90-99), add two fields after `phone_exact_match`/`email_exact_match`:

```python
    phone_exact_match: bool = False
    phone_approx_match: bool = False
    phone_high_fanout: bool = False
    email_exact_match: bool = False
    email_approx_match: bool = False
    dob_exact_match: bool = False
    dob_conflict: bool = False
    address_match: bool = False
    name_similarity: float | None = None
    name_mismatch: bool = False
    identifier_evidence_raw: float = 0.0
    identifier_system_corroborated: bool = False
```

- [ ] **Step 5: Add `_score_approx_identifiers` and wire it into `_score_identifiers`**

In `_score_identifiers` (lines 150-206), change the `return evidence` statement at the end to:

```python
    evidence += _score_approx_identifiers(identifiers, snapshot, cand_phones, cand_emails, reasons, signals)
    return evidence
```

Then add the new function immediately after `_score_identifiers` (before `_cap_identifier_evidence`):

```python
def _score_approx_identifiers(
    identifiers: list[NormalizedIdentifier],
    snapshot: CandidateSnapshot,
    cand_phones: dict[str, RecordDict],
    cand_emails: dict[str, RecordDict],
    reasons: list[str],
    signals: HeuristicSignals,
) -> float:
    """Second pass: near-miss phone/email scoring for non-exact-matched identifiers.

    Weak, corroborating-only evidence (matching-spec "Approximate Identifier
    Matching"). Tracked via separate ``*_approx_match`` signals — distinct from
    ``*_exact_match`` and ``identifier_system_corroborated`` — so it can never
    satisfy a promotion criterion or affect fanout checks.
    """
    evidence = 0.0
    for ident in identifiers:
        if ident.identifier_type == "phone" and ident.normalized_value not in cand_phones:
            if not signals.phone_approx_match and any(
                phone_near_match(ident.normalized_value, str(cand["normalized_value"]))
                for cand in snapshot.phones()
            ):
                evidence += PHONE_APPROX_WEIGHT
                signals.phone_approx_match = True
                reasons.append(f"Phone near-match (NSN edit-distance=1: +{PHONE_APPROX_WEIGHT:.2f})")
        elif ident.identifier_type == "email" and ident.normalized_value not in cand_emails:
            if not signals.email_approx_match and any(
                email_near_match(ident.normalized_value, str(cand["normalized_value"]))
                for cand in snapshot.emails()
            ):
                evidence += EMAIL_APPROX_WEIGHT
                signals.email_approx_match = True
                reasons.append(
                    f"Email near-match (domain/local-part near-miss: +{EMAIL_APPROX_WEIGHT:.2f})"
                )
    return evidence
```

- [ ] **Step 6: Add feature-snapshot fields**

In `_build_feature_snapshot` (lines 295-321), add the two new keys alongside the exact-match flags:

```python
    return {
        "candidate_person_id": candidate_person_id,
        "phone_exact_match": signals.phone_exact_match,
        "phone_approx_match": signals.phone_approx_match,
        "phone_high_fanout": signals.phone_high_fanout,
        "email_exact_match": signals.email_exact_match,
        "email_approx_match": signals.email_approx_match,
        "dob_exact_match": signals.dob_exact_match,
        "dob_conflict": signals.dob_conflict,
        "name_similarity": signals.name_similarity,
        # Strong name mismatch is a hard conflict signal (matching-spec
        # "Conflict signals") — both sides had names and they barely match.
        "name_mismatch": signals.name_mismatch,
        "address_match": signals.address_match,
        # True when a matched identifier is backed by a non-conversation source
        # on the candidate side (independent corroboration for promotion).
        "identifier_system_corroborated": signals.identifier_system_corroborated,
        "identifier_evidence_raw": signals.identifier_evidence_raw,
        "identifier_evidence_capped": min(signals.identifier_evidence_raw, IDENTIFIER_EVIDENCE_CAP),
        "raw_score": raw_score,
        "conversation_promotion": False,
    }
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_heuristic_approx_match.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Run the full matching test suite (regression check)**

Run: `uv run pytest services/ingestion/tests -k "heuristic or match_engine or relationship or conversation" -v`
Expected: PASS — `_has_hard_conflict`, `_promote_by_record_type`, `_apply_promotion`, `_can_promote_conversation` are untouched and only read `*_exact_match`/`identifier_system_corroborated`/`dob_conflict`/`name_mismatch`/`phone_high_fanout`, none of which the new approx fields affect.

- [ ] **Step 9: Lint and type-check**

Run:
```bash
uv run --package profile-unifier-ingestion ruff check services/ingestion/src/matching/heuristic.py services/ingestion/tests/test_heuristic_approx_match.py
uv run --package profile-unifier-ingestion ruff format services/ingestion/src/matching/heuristic.py services/ingestion/tests/test_heuristic_approx_match.py
uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src/matching/heuristic.py
```
Expected: no errors. Note: `heuristic.py` was already ~430 lines (over the ~400-line guideline) before this change; this plan does not split the module — that's a pre-existing condition, out of scope here (like the documented `types_sales.py`/`types_requests.py` exceptions).

- [ ] **Step 10: Commit**

```bash
git add services/ingestion/src/matching/heuristic.py services/ingestion/tests/test_heuristic_approx_match.py
git commit -m "feat(ingestion): score approximate phone/email near-misses in heuristic layer"
```

---

### Task 10: Documentation — matching-spec updates

**Files:**
- Modify: `docs/profile-unifier-matching-spec.md`

- [ ] **Step 1: Add approximate-match bullets to "Positive Evidence"**

In `docs/profile-unifier-matching-spec.md`, find the `### Positive Evidence` list (around line 228-237):

```markdown
### Positive Evidence

- exact verified phone match
- exact verified email match
- DOB exact match
- high full-name similarity
- high address similarity
- same Address node (shared `LIVES_AT` relationship)
- repeated co-occurrence across source updates
- same trusted external ID family
```

Replace with:

```markdown
### Positive Evidence

- exact verified phone match
- exact verified email match
- approximate (near-miss) phone match (same region, NSN edit-distance 1)
- approximate (near-miss) email match (domain-typo or local-part near-miss)
- DOB exact match
- high full-name similarity
- high address similarity
- same Address node (shared `LIVES_AT` relationship)
- repeated co-occurrence across source updates
- same trusted external ID family
```

- [ ] **Step 2: Add a new "Approximate Identifier Matching" subsection**

After the "Per-record-type merge criteria" block and before `## Example Feature Vector` (around line 286-288), find:

```markdown
`identity` keeps the plain additive behaviour with the unconditional NRIC merge.

## Example Feature Vector
```

Replace with:

```markdown
`identity` keeps the plain additive behaviour with the unconditional NRIC merge.

### Approximate Identifier Matching

Two normalization-correctness prerequisites make *exact*-match identifiers more
correct before any approximate scoring runs:

- **Region-hint phone normalization**: eko/speedzone connectors derive an ISO
  region hint from the source row's `phone_code`/`country` columns and pass it
  to `normalize_phone`, falling back to the SG default if the hinted region
  produces `invalid_format`. This resolves the region-ambiguity where an
  8-digit local number normalizes to a different, both-"valid" E.164 value
  depending on default region.
- **Gmail/Googlemail canonicalization**: `john.tan+promo@googlemail.com` and
  `johntan@gmail.com` normalize to the same `normalized_value` (`+tag`
  stripped, dots removed from the local part, domain folded to `gmail.com`) —
  a true equivalence, so these match **exactly** via existing graph traversal,
  not via approximate scoring.

On top of these, the heuristic scorer runs a second pass over incoming
phone/email identifiers that did **not** get an exact match: a same-region
phone whose national significant number is Damerau-Levenshtein distance 1 from
one of the candidate's phones (`phone_approx_match`), or an email that is a
single-axis domain-typo or local-part near-miss of one of the candidate's
emails (`email_approx_match`). Each contributes a small `+0.10` weight.

**Approximate signals are excluded from all promotion paths.** Conversation
promotion (`_can_promote_conversation`) and the `relationship` promotion branch
of `_promote_by_record_type` both key off `phone_exact_match`/`email_exact_match
is True` — `phone_approx_match`/`email_approx_match` are distinct fields that
neither function reads, so an approximate match can never by itself satisfy a
promotion criterion. Approximate evidence is also excluded from
`identifier_system_corroborated` and from fanout checks (fanout concerns the
*candidate's* value being widely shared, not a value that doesn't match it
exactly).

## Example Feature Vector
```

- [ ] **Step 3: Add the new booleans to the Example Feature Vector**

Find the JSON block (around line 290-305):

```json
{
  "phone_exact_match": true,
  "phone_verified_both": false,
  "email_exact_match": false,
  "dob_exact_match": true,
  "dob_conflict": false,
  "name_similarity": 0.82,
  "address_similarity": 0.35,
  "source_trust_left": 0.7,
  "source_trust_right": 0.9,
  "phone_identifier_cardinality": 2,
  "manual_no_match_lock": false,
  "government_id_conflict": false
}
```

Replace with:

```json
{
  "phone_exact_match": true,
  "phone_approx_match": false,
  "phone_verified_both": false,
  "email_exact_match": false,
  "email_approx_match": false,
  "dob_exact_match": true,
  "dob_conflict": false,
  "name_similarity": 0.82,
  "address_similarity": 0.35,
  "source_trust_left": 0.7,
  "source_trust_right": 0.9,
  "phone_identifier_cardinality": 2,
  "manual_no_match_lock": false,
  "government_id_conflict": false
}
```

- [ ] **Step 4: Add the new weights to "Positive Weights"**

Find the `### Positive Weights` list (around line 322-330):

```markdown
### Positive Weights

- verified government ID exact match: hard merge
- exact verified phone: `+0.35`
- exact verified email: `+0.35`
- DOB exact match: `+0.25`
- high name similarity: `+0.20`
- high address similarity: `+0.10`
- trusted source bonus: `+0.05`
```

Replace with:

```markdown
### Positive Weights

- verified government ID exact match: hard merge
- exact verified phone: `+0.35`
- exact verified email: `+0.35`
- approximate phone match: `+0.10`
- approximate email match: `+0.10`
- DOB exact match: `+0.25`
- high name similarity: `+0.20`
- high address similarity: `+0.10`
- trusted source bonus: `+0.05`
```

- [ ] **Step 5: Add new case types to the benchmark "Dataset Requirements" list**

Find the `### Dataset Requirements` list (around line 410-419):

```markdown
### Dataset Requirements

Include:

- true matches
- true non-matches
- ambiguous pairs
- shared family phones
- shared business contact details
- name abbreviation cases
- stale or outdated emails
- conflicting source records
```

Replace with:

```markdown
### Dataset Requirements

Include:

- true matches
- true non-matches
- ambiguous pairs
- shared family phones
- shared business contact details
- name abbreviation cases
- stale or outdated emails
- conflicting source records
- single-digit phone typos and transposed-digit phone numbers (same region)
- gmail dot/plus-addressing variants (e.g. `john.tan+promo@googlemail.com` vs `johntan@gmail.com`)
- common email domain typos (e.g. `gmial.com`, `hotmial.com`, `yahooo.com`)
```

- [ ] **Step 6: Commit**

```bash
git add docs/profile-unifier-matching-spec.md
git commit -m "docs: document approximate identifier matching (Track A/B)"
```

---

### Task 11: Final verification — full ingestion lint/type/test run

**Files:** none (verification only)

- [ ] **Step 1: Run ruff check across the whole ingestion service**

Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src services/ingestion/tests`
Expected: no errors

- [ ] **Step 2: Run ruff format check**

Run: `uv run --package profile-unifier-ingestion ruff format --check services/ingestion/src services/ingestion/tests`
Expected: no changes needed

- [ ] **Step 3: Run mypy --strict across the ingestion source**

Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`
Expected: no errors (modulo the documented pre-existing `types_sales.py`/`types_requests.py` exceptions, unrelated to this change)

- [ ] **Step 4: Run the full ingestion test suite**

Run: `uv run pytest services/ingestion/tests -v`
Expected: PASS — all existing tests plus the ~28 new tests added across Tasks 1-9 pass.

- [ ] **Step 5: Run the full repo test suite (cross-service regression check)**

Run: `uv run pytest`
Expected: PASS — no changes touched `services/api`, so this should be unaffected, but confirms nothing else relies on the modified shared modules in an unexpected way.

No commit for this task — it is a verification-only checkpoint. If any step fails, fix the root cause in the relevant task's files and re-run from Step 1.
