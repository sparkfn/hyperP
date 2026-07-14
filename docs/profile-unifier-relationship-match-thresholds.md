# Relationship match thresholds

## Scope

Apply a dedicated disposition policy to every incoming record whose
`record_type` is `relationship` and to person-pair audits triggered by that
record. Other record types retain their existing thresholds.

## Policy

- Confidence at or above `0.20`: auto-merge.
- Confidence from `0.10` inclusive to `0.20` exclusive: human review.
- Confidence below `0.10`: no match.
- A score conflict (DOB conflict, strong name mismatch, or high-fanout phone)
  vetoes auto-merge. Scores at or above `0.10` go to review; lower scores remain
  no match. An active no-match lock remains an absolute suppression and creates
  neither a merge nor a duplicate review case.

The recorded confidence remains the heuristic score. Threshold selection must
not inflate confidence. Match and pair-audit feature snapshots identify the
relationship policy and its merge/review thresholds for explainability.

## Data flow

The match engine selects disposition thresholds from the incoming record type.
After graph linking, the pipeline passes that record type to person-pair
auditing, which applies the same policy to any shared-identifier pair. Calls
without relationship context preserve the existing pair-audit `0.40` merge and
`0.20` review thresholds.

## Verification

Tests cover `0.099`, `0.10`, `0.199`, and `0.20` boundaries in both paths,
hard-conflict vetoes, and regression behavior for non-relationship records.
