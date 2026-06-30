# Approximate/Partial Identifier Matching (Phone & Email) — Design

**Date**: 2026-06-16
**Status**: Proposed (design only — implementation tracked separately)
**Scope**: `services/ingestion/src/normalizers/`, `services/ingestion/src/matching/`,
`services/ingestion/src/connectors/{eko,speedzone}/`, `docs/profile-unifier-matching-spec.md`

## Purpose

Add **partial/approximate matching for phone and email identifiers** as a new,
weak, corroborating signal in the Layer 2 heuristic scorer — for the data
entry errors (digit typos, transposed digits, email domain typos, missing/extra
dots) that are common in POS and CRM source data and currently produce a
hard non-match on otherwise-identical persons.

Per the precision-over-recall policy (matching-spec, Decision Philosophy), this
is **scoring-only**: approximate matches never create new candidates (candidate
generation is unchanged — graph traversal through shared Identifier/Address
nodes plus DOB+name composite blocking remains the only way a pair is
considered), never satisfy any auto-merge promotion criterion on their own, and
are individually far too small a weight to cross the review threshold (0.60)
unassisted.

## Background: findings from source full dumps

Profiled `.dumps/fundbox_2026-05-06.sql`, `.dumps/eko_phppos_2026-05-06.sql`,
`.dumps/speedzone_phppos_2026-05-06.sql` to ground the "country code / origin"
part of this design in real data:

- **~99% of phone numbers are Singapore mobiles**, entered either as 8-digit
  local (`96427694`) or pre-prefixed with `65` but no `+` (`6596427694`).
  `phonenumbers.parse(..., region="SG")` (the current hardcoded default)
  handles both correctly.
- A real minority are genuinely foreign — Malaysia (`+60...`), Philippines
  (`+63...`), China (`+86...`) — already stored with a leading `+` in fundbox,
  so they normalize correctly today regardless of default region.
- **eko/speedzone's `phppos_people` table carries `country` and `phone_code`
  columns that the connectors already SELECT but never use.** `phone_code`
  values seen: `'65'`, `'93'`, `'60'`, `'63'`, `'39'`, `'91'`, `'44'`, `'62'`.
  `country` values seen (with typo/case noise): `Singapore`, `Singapore `,
  `SINGAPORE`, `SG`, `SIngapore`, ` Singapore `, `Malaysia`, `IT`, `US`.
- **Critical region-ambiguity bug**: an 8-digit local number normalizes to a
  *different, both-"valid"* E.164 value depending on the default region —
  `'96542555'` → `+6596542555` under `SG`, but → `+6096542555` under `MY`. The
  same raw digits from two source systems can become two different Identifier
  nodes (false negative), or — worse — an unrelated MY person's number could
  collide with an SG person's number purely from a wrong default-region guess
  (false-merge risk via exact Identifier traversal).
- Some junk exists too (e.g. `+1116591062706`, which looks like a
  double-prefixed SG number `6591062706`); this normalizes to `INVALID_FORMAT`
  today and is silently dropped. Out of scope for this design — noted for
  future cleanup.
- **Email**: 81% `gmail.com`, then `hotmail.com`/`hotmail.sg`, `yahoo.com`/
  `yahoo.com.sg`, `outlook.com`, `icloud.com`, `live.com`,
  `privaterelay.appleid.com` (Apple relay — excluded, not a stable identifier
  per person), `ada.asia` (internal staff — excluded). 505 of 6579 fundbox
  users' gmail addresses contain dots in the local part — Gmail ignores dots
  and `+tag` suffixes, so `john.tan+promo@gmail.com` and `johntan@gmail.com`
  are the *same mailbox*.

## Track A — Normalization correctness fixes (prerequisite)

These are **not** approximate matching — they make the *exact*-match
normalized values themselves more correct, directly addressing the dump
findings above. Track B (approximate scoring) builds on top of these.

### A1. Region-hint phone normalization

- `normalize_phone(raw, *, region="SG")` (`normalizers/phone.py`) already
  accepts a region override; no connector currently supplies one.
- Add `region_hint: str | None = None` to `RawIdentifier` (`models.py`) and to
  `IdentifierBag.add(...)` (`connectors/fundbox/builders.py`, shared by
  eko/speedzone). `normalize_envelope_identifiers`
  (`pipeline_normalization.py`) passes `region_hint` to
  `normalize_phone(value, region=region_hint)` when `identifier_type ==
  "phone"` and a hint is present; all other identifier types/connectors are
  unaffected.
- eko/speedzone connectors (`connectors/eko/connector.py`,
  `connectors/speedzone/connector.py`) compute the hint from columns they
  already SELECT but discard:
  1. Prefer `phone_code` (numeric calling code, e.g. `"60"`) →
     `phonenumbers.region_code_for_country_code(int(phone_code))` → `"MY"`.
  2. Else fall back to a small `country`-text → ISO-region map covering the
     values actually observed (`singapore`/`sg` variants incl. stray
     whitespace/case → `SG`, `malaysia`/`malaysian` → `MY`, `indonesia` →
     `ID`, `philippines`/`filipino` → `PH`, etc.), matched
     case/whitespace-insensitively.
  3. Else `None` — normalizer falls back to its current `SG` default; no
     behavior change for the ~98% of rows with no usable hint.
- **Fallback chain (never worse than today)**: call
  `normalize_phone(value, region=hint)` first. If it returns
  `INVALID_FORMAT` and `hint != "SG"`, retry with `region="SG"`. Keep
  whichever call succeeds, preferring the hinted-region result if both
  succeed. A noisy/wrong `country`/`phone_code` value can therefore never
  make a number that normalizes fine today start failing.
- `phonenumbers.parse()` only falls back to the supplied `region` when the raw
  string carries no explicit `+`/`00` country code — so passing a hint is safe
  even for already-fully-qualified numbers (e.g. `+8618842549336` stays
  Chinese regardless of hint).
- fundbox connectors pass no `region_hint` (`None`) — fully unaffected.

### A2. Gmail/Googlemail canonicalization

- In `normalize_email` (`normalizers/email.py`), for domains `gmail.com` and
  `googlemail.com`, apply in order:
  1. canonicalize the domain to `gmail.com`,
  2. truncate the local part at the first `+` (drop the `+tag` suffix),
  3. remove all `.` characters from the (now-truncated) local part.
  - e.g. `John.Tan+promo@googlemail.com` → `johntan@gmail.com`.
- This is a true equivalence (Gmail ignores dots and routes `+tag` addresses
  to the base mailbox — same mailbox, same owner), so the canonical form
  becomes the stored `normalized_value`. Two records with
  `john.tan@gmail.com` and `johntan+work@gmail.com` now share the **same**
  Identifier node and match **exactly** via existing graph traversal — no new
  heuristic signal required for this case.
- Quality flag stays `VALID`. Raw values are untouched in `raw_payload`
  (immutable source facts policy is unaffected).

## Track B — Approximate-match scoring algorithms

New module `services/ingestion/src/matching/identifier_similarity.py`
(parallel to `matching/names.py`), pure functions, stdlib-only (consistent with
`matching/similarity.py`).

### B1. Phone digit-typo (`phone_near_match`)

- Inputs: two normalized E.164 phone strings, both already passing
  `is_usable()` (`VALID`/`PARTIAL_PARSE`).
- Derive each number's region via `phonenumbers.parse(value,
  None)` → `phonenumbers.region_code_for_number(...)`. **Require the same
  region for both** — cross-country numbers are never treated as
  near-matches. This avoids the false-positive risk of unrelated numbers in
  different countries coincidentally landing 1 edit apart, and is the direct
  mitigation for the region-ambiguity bug found in the dumps (Track A fixes
  the normalization; this gate stops any residual ambiguity from producing a
  cross-country near-match).
- Extract the national significant number (NSN — digits after the country
  code) for each.
- Compute **Damerau-Levenshtein edit distance** between the two NSNs (new
  helper added to `matching/similarity.py`, alongside the existing
  `jaro_similarity`/`jaro_winkler_similarity`). Distance `== 1` → near-match
  (covers single substitution, adjacent transposition, or a single
  insertion/deletion — the dominant data-entry error modes). Distance `0` is
  already an exact match (handled elsewhere, not by this function); distance
  `>= 2` → no signal.
- Rationale for threshold `1`: for an 8-digit SG mobile, the "1 edit away"
  neighborhood is ~80 numbers out of 10^8 possible — a coincidental match
  between two unrelated people is vanishingly unlikely, so this is safe even
  as a standalone weak signal.

### B2. Email near-miss (`email_near_match`)

- Inputs: two normalized email strings (post Track-A gmail canonicalization),
  both `VALID`.
- A small curated **known-domain list**, derived from the dump's actual top
  domains: `gmail.com`, `hotmail.com`, `hotmail.sg`, `yahoo.com`,
  `yahoo.com.sg`, `outlook.com`, `icloud.com`, `live.com`. (Apple relay and
  internal-staff domains excluded — not meaningful person identifiers.)
- **Exactly one axis may be fuzzy at a time** — never compound two
  approximations, to keep false-positive risk bounded:
  - **Domain-typo axis**: local parts are byte-identical AND the two domains
    differ by Levenshtein distance `1`, with at least one of the two domains
    present in the known-domain list (the list anchors what "correct" looks
    like — e.g. `user@gmial.com` vs `user@gmail.com`).
  - **Local-part-typo axis**: domains are identical (post-canonicalization)
    AND both local parts have length `>= 4` AND Jaro-Winkler similarity
    `>= 0.90` between the local parts.

### B3. Constants summary

| Constant | Value | Rationale |
|---|---|---|
| `PHONE_NSN_EDIT_DISTANCE_THRESHOLD` | `1` | single typo/transposition/insert-delete |
| `EMAIL_DOMAIN_EDIT_DISTANCE_THRESHOLD` | `1` | single-char domain typo |
| `EMAIL_LOCAL_PART_JW_THRESHOLD` | `0.90` | tighter than name-similarity bands (0.80 "high"); local parts are short, a 1-char diff already lands in 0.85-0.95 |
| `EMAIL_LOCAL_PART_MIN_LENGTH` | `4` | avoid Jaro-Winkler inflation on very short local parts |

## Heuristic integration (Layer 2)

New constants in `matching/heuristic.py`:

```python
PHONE_APPROX_WEIGHT = 0.10   # half of PHONE_UNVERIFIED_WEIGHT (0.20)
EMAIL_APPROX_WEIGHT = 0.10   # half of EMAIL_UNVERIFIED_WEIGHT (0.20)
```

**New `HeuristicSignals` fields**: `phone_approx_match: bool`,
`email_approx_match: bool` — distinct from `phone_exact_match`/
`email_exact_match`, deliberately. `_can_promote_conversation` and the
`relationship` branch of `_promote_by_record_type` both key off
`phone_exact_match`/`email_exact_match is True`; using separate fields means
**approximate matches automatically cannot trigger any auto-merge
promotion** — no extra gating code is needed, it falls out of the existing
structure.

**Scoring flow in `_score_identifiers`**:

1. Existing exact-match loop runs unchanged (sets `phone_exact_match`/
   `email_exact_match`, fanout checks, `identifier_system_corroborated`,
   etc.).
2. New second pass: for each incoming `phone`/`email` identifier that did
   **not** get an exact match in step 1, compare against **all** of the
   candidate's identifiers of that type (new `CandidateSnapshot.phones()` /
   `CandidateSnapshot.emails()` accessors returning the value list, alongside
   the existing `phones_by_value()`/`emails_by_value()` dicts) using B1/B2. On
   the first near-match found, add `PHONE_APPROX_WEIGHT` /
   `EMAIL_APPROX_WEIGHT` to the running evidence total, set the corresponding
   signal, and append a reason string, e.g.
   `"Phone near-match (NSN edit-distance=1: +0.10)"`.
3. Approximate evidence flows through the existing `raw_ident_evidence` →
   `_cap_identifier_evidence` (0.85 cap) — no new cap required.
4. **Explicitly excluded** from: `identifier_system_corroborated` (stays
   exact-match-only, so conversation-promotion gating is unaffected by
   approximate matches) and fanout checks (fanout is about the *candidate's*
   value being widely shared — not meaningful for a value that doesn't match
   it exactly).

**Feature snapshot additions** (`_build_feature_snapshot`):
`phone_approx_match`, `email_approx_match` — booleans alongside the existing
exact-match flags, so reviewers/audits can distinguish "matched exactly" vs
"near-miss".

**Why this cannot cause a false auto-merge**: `0.10` is far below
`CONFIDENCE_REVIEW` (0.60) on its own, never sets
`identifier_system_corroborated`, and satisfies none of the
`_promote_by_record_type` criteria (all require `*_exact_match`). The pair
must already have reached this candidate via an existing candidate-generation
path (exact identifier, address, or DOB+name composite blocking) — Track B
introduces no new candidate-generation path.

## Testing & validation plan

- **Unit tests** for `identifier_similarity.phone_near_match` /
  `email_near_match`: same-region NSN edit-distance-1 (substitution,
  transposition, insertion, deletion) → match; edit-distance-2 → no match;
  cross-region edit-distance-1 → no match; gmail-canonicalized equality →
  exact (not "near"); single domain-typo with exact local part → match;
  local-part JW >= 0.90 with identical domain → match; both axes fuzzy
  simultaneously → no match.
- **Unit tests** for the new Damerau-Levenshtein helper in
  `matching/similarity.py`, mirroring the existing `jaro_*` test style.
- **Unit tests** for A1 (region-hint normalization): eko/speedzone rows with
  `phone_code='60'`/`country='Malaysia'` normalize MY local numbers
  correctly; rows with no hint behave exactly as today; a row whose hinted
  region produces `INVALID_FORMAT` falls back to `SG` and matches today's
  result.
- **Unit tests** for A2 (gmail canonicalization): dot/plus variants of the
  same mailbox normalize to the same `normalized_value`; non-gmail domains
  unaffected.
- **Heuristic integration tests** in `services/ingestion/tests/matching/`:
  a candidate reachable via DOB+name blocking, with a 1-digit-typo phone vs.
  the candidate's phone, scores `+0.10` and lands in `feature_snapshot` as
  `phone_approx_match: true`; confirm a conversation-record pair whose *only*
  evidence is an approximate phone match stays capped at `no_match`/`review`
  (never promoted).
- **Benchmark dataset** (matching-spec "Dataset Requirements"): add cases for
  single-digit phone typos, transposed digits, gmail dot/plus variants, and
  common email domain typos (`gmial.com`, `hotmial.com`, `yahooo.com`).

## Documentation updates (both in `docs/profile-unifier-matching-spec.md`)

- **Heuristic Feature Catalog → Positive Evidence**: add "approximate
  (near-miss) phone match (same region, NSN edit-distance 1)" and
  "approximate (near-miss) email match (domain-typo or local-part
  near-miss)".
- **Positive Weights**: add `approximate phone match: +0.10` and
  `approximate email match: +0.10`.
- **Example Feature Vector**: add `phone_approx_match`/`email_approx_match`
  booleans.
- New short subsection documenting Track A's normalization prerequisites
  (region-hint phone parsing, gmail canonicalization) and stating explicitly
  that approximate signals are excluded from all promotion paths
  (conversation promotion, `relationship` promotion).

## Out of scope / future work

- **Cross-region phone suffix matching** (e.g. catching a number that was
  normalized under the wrong default region *before* Track A landed, or for
  fundbox which has no region hints at all). Track A's fallback chain
  addresses the known eko/speedzone cases; a general cross-region heuristic
  would need much higher false-positive guardrails (e.g. requiring an
  independent corroborating signal such as exact DOB match before
  contributing any weight) and is deferred until there's evidence of residual
  need.
- **Fuzzy candidate generation** (a phone/email "fingerprint" index so an
  approximate match can surface a *new* candidate person, not just score an
  existing one) — explicitly deferred per the scoping decision in this design
  session; would be a phase-2 extension once this scoring-only approach is
  validated on real data.
- **Junk-prefix phone salvage** (e.g. `+1116591062706` →
  `+6591062706`) — noted from the dump analysis but unrelated to approximate
  *matching*; a separate normalization-cleanup effort.
