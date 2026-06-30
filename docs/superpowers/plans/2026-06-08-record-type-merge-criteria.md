# Per-record-type merge criteria Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `bankruptcy` NRIC+name deterministic gate and a `relationship` phone+name heuristic auto-merge trigger, sharing one name-match helper and a generalized record-type promotion path. No behaviour change for `identity`, `conversation`, or `sales`.

**Architecture:** (1) extract a shared name-similarity helper; (2) gate the bankruptcy NRIC hard-merge on a partial name in `deterministic.py`; (3) generalize conversation promotion into a per-record-type dispatch in `heuristic.py` and add the relationship rule, guarded by a shared hard-conflict check.

**Tech Stack:** Python 3.12 (uv `profile-unifier-ingestion`), Neo4j. pytest; ruff + mypy --strict.

**Commit note:** Standing rule — never commit without explicit user instruction. `Commit` steps below are the intended rhythm; get an explicit go-ahead first.

---

### Task 1: Shared name-match helper in `similarity.py`

**Files:**
- Modify: `services/ingestion/src/matching/similarity.py`
- Modify: `services/ingestion/src/matching/heuristic.py` (refactor `_score_name`, re-point `NAME_MISMATCH_THRESHOLD`)
- Test: `services/ingestion/tests/test_name_match_helper.py` (new)

- [ ] **Step 1: Write the failing test**

Create `services/ingestion/tests/test_name_match_helper.py`:

```python
"""Shared name-match helper used by both the deterministic gate and heuristic."""

from __future__ import annotations

from src.matching.similarity import (
    NAME_PARTIAL_THRESHOLD,
    best_name_similarity,
    incoming_names,
    is_partial_name_match,
)
from src.models import NormalizedAttribute, QualityFlag


def _attr(name: str, value: str) -> NormalizedAttribute:
    return NormalizedAttribute(attribute_name=name, attribute_value=value, quality_flag=QualityFlag.VALID)


def test_incoming_names_picks_name_fields_only() -> None:
    attrs = [_attr("full_name", "Ada Lovelace"), _attr("dob", "1990-01-01")]
    assert incoming_names(attrs) == ["Ada Lovelace"]


def test_best_similarity_is_one_for_exact() -> None:
    assert best_name_similarity(["Ada Lovelace"], ["Ada Lovelace"]) == 1.0


def test_partial_match_true_above_threshold() -> None:
    assert is_partial_name_match([_attr("full_name", "Li Wei")], ["Wei Li"]) is True
    assert NAME_PARTIAL_THRESHOLD == 0.50


def test_partial_match_false_for_strong_mismatch() -> None:
    assert is_partial_name_match([_attr("full_name", "Ada Lovelace")], ["Zhang Qiang"]) is False


def test_partial_match_none_when_a_side_lacks_name() -> None:
    assert is_partial_name_match([], ["Ada Lovelace"]) is None
    assert is_partial_name_match([_attr("full_name", "Ada")], []) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_name_match_helper.py -q`
Expected: FAIL — `ImportError` (helpers not defined).

- [ ] **Step 3: Add the helpers to `similarity.py`**

Append to `services/ingestion/src/matching/similarity.py` (add the `NormalizedAttribute` import at the top):

```python
from src.models import NormalizedAttribute

#: Incoming attribute names treated as a person's name for matching.
NAME_INCOMING_FIELDS: tuple[str, ...] = ("full_name", "preferred_name", "legal_name")
#: Jaro-Winkler at/above this is a partial-or-better name match; below it is a
#: strong mismatch. Single source of truth shared by the deterministic gate and
#: the heuristic name band.
NAME_PARTIAL_THRESHOLD: float = 0.50


def incoming_names(attributes: list[NormalizedAttribute]) -> list[str]:
    """Return the name strings carried by an incoming record's attributes."""
    return [a.attribute_value for a in attributes if a.attribute_name in NAME_INCOMING_FIELDS]


def best_name_similarity(incoming: list[str], candidate_names: list[str]) -> float:
    """Best Jaro-Winkler similarity across the incoming × candidate name pairs."""
    best = 0.0
    for inc in incoming:
        for cand in candidate_names:
            best = max(best, jaro_winkler_similarity(inc, cand))
    return best


def is_partial_name_match(
    attributes: list[NormalizedAttribute],
    candidate_names: list[str],
) -> bool | None:
    """Partial-name verdict for a pair.

    Returns ``True``/``False`` when both sides carry a name (JW ≥/<
    ``NAME_PARTIAL_THRESHOLD``); ``None`` when either side has no name, so callers
    can decide a name-absent fallback.
    """
    inc = incoming_names(attributes)
    if not inc or not candidate_names:
        return None
    return best_name_similarity(inc, candidate_names) >= NAME_PARTIAL_THRESHOLD
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_name_match_helper.py -q`
Expected: PASS.

- [ ] **Step 5: Refactor `heuristic._score_name` to reuse the helper (no behaviour change)**

In `services/ingestion/src/matching/heuristic.py`:
- Add to the existing `from src.matching.similarity import ...`: `best_name_similarity, incoming_names, NAME_PARTIAL_THRESHOLD`.
- Replace the body of `_score_name` so it builds `incoming = incoming_names(attributes)` and returns `best_name_similarity(incoming, snapshot.names())` (preserving the early `return 0.0` when either is empty).
- Replace the literal `NAME_MISMATCH_THRESHOLD = 0.50` definition with `NAME_MISMATCH_THRESHOLD = NAME_PARTIAL_THRESHOLD` (keep the explanatory comment).

- [ ] **Step 6: Run the heuristic regression to confirm no behaviour change**

Run: `uv run pytest services/ingestion/tests -k "heuristic or name or match_engine" -q`
Expected: PASS (existing heuristic tests unaffected).

- [ ] **Step 7: Commit** (after explicit go-ahead)

```bash
git add services/ingestion/src/matching/similarity.py services/ingestion/src/matching/heuristic.py services/ingestion/tests/test_name_match_helper.py
git commit -m "refactor(matching): extract shared name-match helper"
```

---

### Task 2: `bankruptcy` NRIC+name deterministic gate

**Files:**
- Modify: `services/ingestion/src/matching/deterministic.py`
- Modify: `services/ingestion/src/matching/engine.py:84-104` (thread `attributes`)
- Test: `services/ingestion/tests/test_bankruptcy_name_gate.py` (new); update `tests/test_match_engine_system_family.py`

- [ ] **Step 1: Write the failing gate test**

Create `services/ingestion/tests/test_bankruptcy_name_gate.py`:

```python
"""Bankruptcy NRIC merge is gated on a partial name when both sides are named."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.deterministic import evaluate_deterministic
from src.models import (
    MatchDecision,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _Tx:
    """Candidate person-1 has a VALID matching NRIC and a full_name fact 'Ada Lovelace'."""

    def __init__(self, candidate_name: str | None) -> None:
        self._candidate_name = candidate_name

    def run(self, query: str, **_params: object) -> _Result:
        if "rel.quality_flag = 'valid'" in query and "person_id AS person_id" in query:
            return _Result([{"person_id": "person-1"}])
        if "conflicting_value" in query:
            return _Result([])
        if "owner_person_id" in query:
            return _Result([])
        if "is_locked" in query:
            return _Result([{"is_locked": False}])
        if "[f:HAS_FACT]->" in query:
            if self._candidate_name is None:
                return _Result([])
            return _Result([{"attribute_name": "full_name", "attribute_value": self._candidate_name,
                             "source_trust_tier": 1, "observed_at": None, "quality_flag": "valid"}])
        return _Result([])


def _nric() -> list[NormalizedIdentifier]:
    return [NormalizedIdentifier(identifier_type="nric", normalized_value="S1234567A",
                                 is_verified=True, quality_flag=QualityFlag.VALID)]


def _name(value: str) -> list[NormalizedAttribute]:
    return [NormalizedAttribute(attribute_name="full_name", attribute_value=value,
                                quality_flag=QualityFlag.VALID)]


def test_bankruptcy_merges_on_nric_plus_partial_name() -> None:
    res = evaluate_deterministic(_Tx("Ada Lovelace"), "person-1", _nric(), _name("Ada Lovelace"), RecordType.BANKRUPTCY)
    assert res is not None and res.decision == MatchDecision.MERGE


def test_bankruptcy_merges_on_nric_when_no_incoming_name() -> None:
    res = evaluate_deterministic(_Tx("Ada Lovelace"), "person-1", _nric(), [], RecordType.BANKRUPTCY)
    assert res is not None and res.decision == MatchDecision.MERGE


def test_bankruptcy_blocks_nric_merge_on_name_conflict() -> None:
    res = evaluate_deterministic(_Tx("Zhang Qiang"), "person-1", _nric(), _name("Ada Lovelace"), RecordType.BANKRUPTCY)
    assert res is None  # falls through to heuristic


def test_identity_still_merges_on_nric_with_conflicting_name() -> None:
    res = evaluate_deterministic(_Tx("Zhang Qiang"), "person-1", _nric(), _name("Ada Lovelace"), RecordType.IDENTITY)
    assert res is not None and res.decision == MatchDecision.MERGE
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_bankruptcy_name_gate.py -q`
Expected: FAIL — `evaluate_deterministic` takes 4 positional args, not 5 (no `attributes`).

- [ ] **Step 3: Add `attributes` + the gate to `deterministic.py`**

In `services/ingestion/src/matching/deterministic.py`:
- Add imports: `from src.matching.similarity import is_partial_name_match` and `from src.matching.snapshot import fetch_candidate_snapshot`; add `NormalizedAttribute` to the `src.models` import.
- Change `evaluate_deterministic` signature to:

```python
def evaluate_deterministic(
    tx: ManagedTransaction,
    candidate_person_id: str,
    identifiers: list[NormalizedIdentifier],
    attributes: list[NormalizedAttribute],
    record_type: RecordType,
) -> MatchResult | None:
```

- Pass `attributes` and `record_type` into `_check_government_id`, and in its MERGE branch (after confirming the candidate shares the valid NRIC, before returning MERGE) insert the bankruptcy gate:

```python
        if record_type == RecordType.BANKRUPTCY:
            verdict = is_partial_name_match(
                attributes, fetch_candidate_snapshot(tx, candidate_person_id).names()
            )
            if verdict is False:
                logger.info(
                    "Bankruptcy NRIC match for candidate %s blocked: name conflict",
                    candidate_person_id,
                )
                return None  # fall through to heuristic (→ no-match / pair review)
        # verdict True (partial name) or None (name absent) → NRIC merge proceeds
```

Keep the conflicting-NRIC NO_MATCH branch and the trusted-ID logic unchanged. (`_check_government_id` now needs `attributes` and `record_type` params — add them and pass through from `evaluate_deterministic`.)

- [ ] **Step 4: Thread `attributes` from the engine**

In `services/ingestion/src/matching/engine.py`, `_evaluate_one` calls `evaluate_deterministic(tx, candidate_person_id, identifiers, record_type)`. Change it to:

```python
        det = evaluate_deterministic(
            tx,
            candidate_person_id,
            identifiers,
            attributes,
            record_type,
        )
```

- [ ] **Step 5: Update the system-family test's deterministic calls**

In `services/ingestion/tests/test_match_engine_system_family.py`, the two direct `evaluate_deterministic(tx, "person-1", _nric(), rt)` calls (in `test_deterministic_nric_merge_identical_across_system_family` and `test_non_system_family_does_not_deterministically_merge_on_nric`) must pass `attributes`:

```python
        evaluate_deterministic(tx, "person-1", _nric(), [], rt)  # type: ignore[arg-type]
```

(Empty attributes → bankruptcy takes the NRIC-alone fallback, so the cross-family equivalence still holds.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest services/ingestion/tests/test_bankruptcy_name_gate.py services/ingestion/tests/test_match_engine_system_family.py -q`
Expected: the bankruptcy gate tests PASS; the deterministic system-family tests PASS. (The heuristic equivalence test may still pass here — it is updated in Task 3.)

- [ ] **Step 7: Commit** (after explicit go-ahead)

```bash
git add services/ingestion/src/matching/deterministic.py services/ingestion/src/matching/engine.py services/ingestion/tests/test_bankruptcy_name_gate.py services/ingestion/tests/test_match_engine_system_family.py
git commit -m "feat(matching): gate bankruptcy NRIC merge on a partial name match"
```

---

### Task 3: `relationship` phone+name promotion + generalized dispatch

**Files:**
- Modify: `services/ingestion/src/matching/heuristic.py`
- Test: `services/ingestion/tests/test_relationship_promotion.py` (new); update `tests/test_match_engine_system_family.py`

- [ ] **Step 1: Write the failing promotion test**

Create `services/ingestion/tests/test_relationship_promotion.py`:

```python
"""Relationship records auto-merge on phone + partial name, blocked by conflicts."""

from __future__ import annotations

from collections.abc import Iterator

from src.matching.engine import MatchEngine
from src.models import (
    CandidateResult,
    MatchDecision,
    MatchResult,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)


class _Result:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._records)

    def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None


class _Tx:
    def __init__(self, *, cand_name: str = "Ada Lovelace", cand_dob: str | None = None, fanout: int = 1) -> None:
        self._cand_name = cand_name
        self._cand_dob = cand_dob
        self._fanout = fanout

    def run(self, query: str, **_params: object) -> _Result:
        if "is_locked" in query:
            return _Result([{"is_locked": False}])
        if "owner_person_id" in query:
            return _Result([])
        if "AS person_id" in query and "LIMIT 1" in query:
            return _Result([])
        if "conflicting_value" in query:
            return _Result([])
        if "[rel:IDENTIFIED_BY]->" in query:
            return _Result([{"identifier_type": "phone", "normalized_value": "+6512345678",
                             "is_verified": False, "last_confirmed_at": None}])
        if "[f:HAS_FACT]->" in query:
            facts: list[dict[str, object]] = [{"attribute_name": "full_name", "attribute_value": self._cand_name,
                                               "source_trust_tier": 1, "observed_at": None, "quality_flag": "valid"}]
            if self._cand_dob is not None:
                facts.append({"attribute_name": "dob", "attribute_value": self._cand_dob,
                              "source_trust_tier": 1, "observed_at": None, "quality_flag": "valid"})
            return _Result(facts)
        if "[rel:LIVES_AT]->" in query:
            return _Result([])
        if "RETURN count(p) AS fanout" in query:
            return _Result([{"fanout": self._fanout}])
        return _Result([])


def _evaluate(tx: _Tx, *, record_type: RecordType, incoming_dob: str | None = None) -> MatchResult:
    attrs = [NormalizedAttribute(attribute_name="full_name", attribute_value="Ada Lovelace", quality_flag=QualityFlag.VALID)]
    if incoming_dob is not None:
        attrs.append(NormalizedAttribute(attribute_name="dob", attribute_value=incoming_dob, quality_flag=QualityFlag.VALID))
    return MatchEngine().evaluate(
        tx,  # type: ignore[arg-type]
        [CandidateResult(person_id="person-1")],
        [NormalizedIdentifier(identifier_type="phone", normalized_value="+6512345678",
                              is_verified=False, quality_flag=QualityFlag.VALID)],
        None,
        attrs,
        record_type=record_type,
    )


def test_relationship_promotes_on_phone_plus_name() -> None:
    res = _evaluate(_Tx(), record_type=RecordType.RELATIONSHIP)
    assert res.decision == MatchDecision.MERGE
    assert any("promot" in r.lower() for r in res.reasons)


def test_identity_not_promoted_same_inputs() -> None:
    res = _evaluate(_Tx(), record_type=RecordType.IDENTITY)
    assert res.decision != MatchDecision.MERGE


def test_relationship_blocked_by_dob_conflict() -> None:
    res = _evaluate(_Tx(cand_dob="1980-02-02"), record_type=RecordType.RELATIONSHIP, incoming_dob="1990-01-01")
    assert res.decision != MatchDecision.MERGE


def test_relationship_blocked_by_high_fanout_phone() -> None:
    res = _evaluate(_Tx(fanout=9), record_type=RecordType.RELATIONSHIP)
    assert res.decision != MatchDecision.MERGE


def test_relationship_blocked_by_name_mismatch() -> None:
    res = _evaluate(_Tx(cand_name="Zhang Qiang"), record_type=RecordType.RELATIONSHIP)
    assert res.decision != MatchDecision.MERGE
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest services/ingestion/tests/test_relationship_promotion.py -q`
Expected: FAIL — relationship is not promoted yet (`test_relationship_promotes_on_phone_plus_name` returns REVIEW).

- [ ] **Step 3: Generalize promotion in `heuristic.py`**

In `services/ingestion/src/matching/heuristic.py`:
- Rename the constant `CONVERSATION_PROMOTED_CONFIDENCE = 0.91` to `PROMOTED_CONFIDENCE = 0.91` and add `CONVERSATION_PROMOTED_CONFIDENCE = PROMOTED_CONFIDENCE` directly below (back-compat alias).
- Add the shared blocker helper:

```python
def _has_hard_conflict(features: dict[str, JsonValue]) -> bool:
    """Conflict signals that veto any record-type promotion."""
    return (
        features["dob_conflict"] is True
        or features["name_mismatch"] is True
        or features["phone_high_fanout"] is True
    )
```

- Replace the call `confidence = _promote_conversation_confidence(record_type, confidence, reasons, features)` with `confidence = _promote_by_record_type(record_type, confidence, reasons, features)` and define:

```python
def _promote_by_record_type(
    record_type: RecordType,
    confidence: float,
    reasons: list[str],
    features: dict[str, JsonValue],
) -> float:
    if confidence >= CONFIDENCE_AUTO_MERGE:
        return confidence
    if record_type == RecordType.CONVERSATION:
        if _can_promote_conversation(features):
            return _apply_promotion(confidence, reasons, features, "conversation")
        return confidence
    if record_type == RecordType.RELATIONSHIP:
        phone = features["phone_exact_match"] is True
        partial_name = _float_feature(features.get("name_similarity")) >= NAME_PARTIAL_THRESHOLD
        if phone and partial_name and not _has_hard_conflict(features):
            return _apply_promotion(confidence, reasons, features, "relationship")
        return confidence
    return confidence


def _apply_promotion(
    confidence: float,
    reasons: list[str],
    features: dict[str, JsonValue],
    label: str,
) -> float:
    features["conversation_promotion"] = label == "conversation"
    features["promotion"] = label
    features["pre_promotion_confidence"] = confidence
    reasons.append(f"{label.capitalize()} evidence promoted to merge")
    return PROMOTED_CONFIDENCE
```

- Refactor `_can_promote_conversation` to defer the conflict check to `_has_hard_conflict(features)` (replace its `not dob_conflict and not name_mismatch and not high_fanout_phone` tail with `and not _has_hard_conflict(features)`), keeping its corroboration logic. Remove the now-unused local conflict variables. Delete the old `_promote_conversation_confidence` (its body moves into the dispatch). Add `NAME_PARTIAL_THRESHOLD` to the `similarity` import if not already present.

Note: the existing conversation reason string is "Conversation evidence promoted to merge" — `_apply_promotion("conversation")` reproduces it exactly, so conversation behaviour and its `conversation_promotion` feature flag are unchanged.

- [ ] **Step 4: Run the promotion test to verify it passes**

Run: `uv run pytest services/ingestion/tests/test_relationship_promotion.py -q`
Expected: PASS.

- [ ] **Step 5: Update the system-family heuristic equivalence test**

In `services/ingestion/tests/test_match_engine_system_family.py`, `test_heuristic_result_identical_across_system_family` now over-asserts (relationship promotes). Replace it with an assertion that `identity` and `bankruptcy` are identical and `relationship` promotes to MERGE:

```python
def test_heuristic_identity_and_bankruptcy_identical_relationship_promotes() -> None:
    identity = _evaluate(RecordType.IDENTITY)
    bankruptcy = _evaluate(RecordType.BANKRUPTCY)
    _assert_all_equal([identity, bankruptcy])
    assert "Conversation evidence promoted to merge" not in identity.reasons

    relationship = _evaluate(RecordType.RELATIONSHIP)
    assert relationship.decision == MatchDecision.MERGE
    assert any("promot" in r.lower() for r in relationship.reasons)
```

Update the module docstring to note that `bankruptcy` matches like `identity` except for the NRIC name-gate, and `relationship` adds a phone+name promotion.

- [ ] **Step 6: Run the full suite + type/lint**

Run: `uv run pytest services/ingestion/tests -q`
Expected: PASS.
Run: `uv run --package profile-unifier-ingestion ruff check services/ingestion/src/matching`
Run: `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`
Expected: both clean.

- [ ] **Step 7: Commit** (after explicit go-ahead)

```bash
git add services/ingestion/src/matching/heuristic.py services/ingestion/tests/test_relationship_promotion.py services/ingestion/tests/test_match_engine_system_family.py
git commit -m "feat(matching): auto-merge relationship records on phone + partial name"
```

---

### Task 4: Documentation

**Files:**
- Modify: `CLAUDE.md` (Multi-match / record-type design notes)
- Modify: `docs/profile-unifier-matching-spec.md` (per-record-type criteria)

- [ ] **Step 1: Update the canonical matching spec**

In `docs/profile-unifier-matching-spec.md`, add a "Per-record-type merge criteria" note: `bankruptcy` requires NRIC + partial name (JW ≥ 0.50) for the deterministic merge, falling back to NRIC-alone when a name is absent; `relationship` auto-merges on phone + partial name (Layer-2 promotion) guarded by the shared blockers (DOB conflict, strong name mismatch, high-fanout phone, plus the deterministic NRIC anti-match / lock). Locate the section with `grep -n "conversation" docs/profile-unifier-matching-spec.md` and place it alongside the conversation-promotion description.

- [ ] **Step 2: Update `CLAUDE.md`**

In the "Match engine review policy" / record-type design notes, note the `bankruptcy` NRIC+name gate and the generalized `_promote_by_record_type` (conversation + relationship today; sales reserved for Spec 3).

- [ ] **Step 3: Commit** (after explicit go-ahead)

```bash
git add CLAUDE.md docs/profile-unifier-matching-spec.md docs/superpowers/specs/2026-06-08-record-type-merge-criteria-design.md docs/superpowers/plans/2026-06-08-record-type-merge-criteria.md
git commit -m "docs: per-record-type merge criteria (bankruptcy gate, relationship trigger)"
```

---

## Self-Review

**Spec coverage:** §Change 1 (shared helper) → Task 1; §Change 2 (bankruptcy gate) → Task 2; §Change 3 (relationship promotion) → Task 3; §Change 4 (tests) → embedded in Tasks 1-3; docs → Task 4. ✓

**Placeholder scan:** All test bodies and implementation snippets are concrete. The two `grep -n` steps are locate-only; the change text is fully specified. No TBD/TODO.

**Type/name consistency:** `NAME_PARTIAL_THRESHOLD` (0.50) defined in `similarity.py` and reused as `heuristic.NAME_MISMATCH_THRESHOLD`. `evaluate_deterministic(tx, pid, identifiers, attributes, record_type)` — the new `attributes` parameter is added in Task 2 Step 3, threaded in Step 4 (engine), and reflected in the test calls in Step 5 and Task 2's own tests. `PROMOTED_CONFIDENCE` replaces `CONVERSATION_PROMOTED_CONFIDENCE` (kept as alias). `_promote_by_record_type` / `_apply_promotion` / `_has_hard_conflict` are introduced and called consistently. The feature flag `promotion` is new; `conversation_promotion` is preserved for back-compat.
```
