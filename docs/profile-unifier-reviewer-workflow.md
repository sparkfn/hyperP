# Profile Unifier Reviewer Workflow

## Purpose

Define the operational review workflow for ambiguous identity decisions. This
document specifies queue states, reviewer actions, permissions, side effects,
locking behavior, and escalation paths so the platform can be implemented with
consistent operational behavior.

## Scope

This workflow covers:

- review-case creation
- assignment and claiming
- merge, reject, defer, and escalate actions
- manual no-match lock creation
- interaction with merge and unmerge flows
- audit and recomputation side effects

It does not cover:

- general support-agent read workflows
- source-system ingestion behavior
- downstream synchronization details

## Design Principles

- review exists to prevent unsafe auto-merges
- every reviewer action must be explicit and auditable
- side effects must be deterministic and replay-safe
- locks and overrides must survive reprocessing
- ambiguous cases should degrade toward safety, not automation

## Actors

### Reviewer

Can:

- claim review cases
- inspect candidate evidence
- merge
- reject
- defer
- escalate
- create manual no-match locks

Cannot:

- perform unmerge directly unless also granted admin role
- bypass hard security restrictions on sensitive raw payloads

### Admin

Can do everything a reviewer can, plus:

- force merge
- unmerge
- remove locks
- resolve exceptional concurrency conflicts
- override queue ownership if needed

### System

Can:

- create review cases
- reprioritize cases
- close stale cases under deterministic conditions
- trigger recomputation and audit events after approved actions

## Workflow Entry Points

A review case should be created when any of the following occurs:

1. a heuristic score falls into the review band
2. an LLM recommendation is present for a review-band case
3. sensitive conflicts exist without meeting a hard block
4. manual policy requires review for a source combination or customer segment
5. the profile is high-value or high-risk and policy forbids auto-merge

## Review Case Data Requirements

Every review case should include:

- `review_case_id`
- linked `match_decision_id`
- queue state
- priority
- SLA due time
- assignment metadata
- comparison payload for candidate entities
- reasons and blocking conflicts from the engine
- prior reviewer actions for the same pair if any
- active locks relevant to the pair

## Review Case States

Recommended canonical states:

- `open`
- `assigned`
- `deferred`
- `resolved`
- `cancelled`

## State Meanings

### open

The case is available for work and not currently owned.

### assigned

The case is actively owned by a reviewer or admin.

### deferred

The case cannot be resolved immediately and requires additional information,
time, or a dependent event.

### resolved

The case has a final operational outcome such as merge, reject, or manual
no-match.

### cancelled

The case is no longer actionable because it has been superseded, invalidated,
or closed automatically.

## Resolution Values

When a case is resolved, the resolution should be one of:

- `merge`
- `reject`
- `manual_no_match`
- `escalated_to_admin`
- `cancelled_superseded`

`defer` should not be treated as a final resolution.

## State Machine

### Allowed Transitions

| From | Action | To |
| --- | --- | --- |
| `open` | assign | `assigned` |
| `open` | defer | `deferred` |
| `open` | merge | `resolved` |
| `open` | reject | `resolved` |
| `open` | manual_no_match | `resolved` |
| `open` | escalate | `assigned` |
| `open` | cancel | `cancelled` |
| `assigned` | unassign | `open` |
| `assigned` | defer | `deferred` |
| `assigned` | merge | `resolved` |
| `assigned` | reject | `resolved` |
| `assigned` | manual_no_match | `resolved` |
| `assigned` | escalate | `assigned` |
| `assigned` | cancel | `cancelled` |
| `deferred` | reopen | `open` |
| `deferred` | assign | `assigned` |
| `deferred` | cancel | `cancelled` |

### Invalid Transitions

- `resolved -> open`
- `resolved -> assigned`
- `cancelled -> open`
- `cancelled -> assigned`

Any such transition should require creating a new review case instead.

## Action Definitions

## assign

Purpose:

- claim a case for active handling

Allowed roles:

- reviewer
- admin

Preconditions:

- case state must be `open` or `deferred`

Side effects:

- set `queue_state = assigned`
- set `assigned_to`
- create `review_action` event

## unassign

Purpose:

- release ownership back to the queue

Allowed roles:

- assigned reviewer
- admin

Preconditions:

- case state must be `assigned`

Side effects:

- set `queue_state = open`
- clear `assigned_to`
- create `review_action`

## merge

Purpose:

- confirm the candidate entities represent the same person

Allowed roles:

- reviewer
- admin

Preconditions:

- case state must be `open` or `assigned`
- no active hard no-match lock on the pair
- hard blockers must not exist

Side effects:

1. create `review_action`
2. create `merge_event` with `manual_merge` or reviewer-approved merge metadata
3. relink affected source records to the target person
4. update absorbed person status if a person-to-person merge occurs
5. close the review case with `queue_state = resolved` and `resolution = merge`
6. recompute golden profile for all affected persons
7. emit audit and downstream domain events if configured

## reject

Purpose:

- reject the suggested match without creating a persistent no-match lock

Allowed roles:

- reviewer
- admin

Preconditions:

- case state must be `open` or `assigned`

Side effects:

1. create `review_action`
2. optionally create `merge_event` of type `review_reject`
3. set review case to `resolved`
4. do not create a persistent pair lock

Use `reject` when:

- current evidence is insufficient
- data may change later
- the pair should remain eligible for future candidate generation

## manual_no_match

Purpose:

- reject the match and persist a lock to suppress future repeated suggestions

Allowed roles:

- reviewer
- admin

Preconditions:

- case state must be `open` or `assigned`

Side effects:

1. create `review_action`
2. create `person_pair_lock` or source-record pair lock
3. create `merge_event` of type `manual_no_match`
4. set review case to `resolved`
5. suppress future candidate generation for the locked pair until expiration or removal

Use `manual_no_match` when:

- the reviewer is confident the pair should not be re-suggested
- the pair involves shared business or family identifiers
- repeat false positives are operationally costly

## defer

Purpose:

- pause action because a decision cannot be made yet

Allowed roles:

- reviewer
- admin

Side effects:

- set `queue_state = deferred`
- preserve `assigned_to` only if policy wants sticky ownership, otherwise clear it
- record defer reason in `review_action`
- optionally set `follow_up_at`

Typical reasons:

- waiting for upstream data refresh
- waiting for manual customer confirmation
- waiting for admin or compliance guidance

## escalate

Purpose:

- route a difficult or sensitive case to a higher-authority owner

Allowed roles:

- reviewer
- admin

Side effects:

- create `review_action`
- mark escalation metadata
- optionally change owner or queue
- keep state as `assigned` if immediately handed off

Escalation should be used for:

- sensitive identifier conflicts with policy ambiguity
- high-value or high-risk profiles
- cases that may require unmerge or broad rollback

## cancel

Purpose:

- close a case that is no longer actionable

Allowed roles:

- system
- admin

Typical reasons:

- superseded by another case
- candidate entities already merged or locked elsewhere
- source data was retracted or suppressed

## Reviewer Decision Guidance

## When to Merge

Merge when:

- there are no hard blockers
- evidence is sufficient and coherent
- the merge is consistent with policy and source trust
- the reviewer can explain the decision in one or two clear reasons

## When to Reject

Reject when:

- evidence is insufficient right now
- the system over-suggested similarity
- you do not want a permanent suppression yet

## When to Create Manual No-Match

Create a persistent lock when:

- you are confident the pair should not recur
- the pair has repeatedly surfaced as a false positive
- a shared phone or email pattern makes repeat false positives likely

## When to Defer

Defer when:

- there is a realistic chance more data will resolve the case soon
- business confirmation is pending
- the case is blocked on external input

## When to Escalate

Escalate when:

- policy interpretation is unclear
- a high-value customer is involved
- there may be fraud, legal, or compliance implications
- the case suggests a systemic rules problem rather than a one-off decision

## Concurrency Rules

Review actions must handle concurrent users safely.

### Recommended Concurrency Model

- use optimistic concurrency with review-case version or `updated_at` checks
- reject stale writes with `409 Conflict`
- allow admins to force reassignment where needed

### Examples

- if reviewer A resolves a case after reviewer B already resolved it, reviewer A should receive `409 Conflict`
- if a lock is created by another action before merge submission, merge should fail with `merge_blocked`

## Queue Ownership Policy

Recommended default:

- `open`: no owner
- `assigned`: single explicit owner
- `deferred`: no owner unless the team prefers sticky ownership

Recommended SLA handling:

- priority and SLA should be recalculated when a case is reopened or deferred
- overdue cases should be surfaced to reviewers and admins distinctly

## Comparison Payload for Review UI

The reviewer UI should display:

- left and right candidate entities
- identifiers with verification and quality flags
- names, DOB, addresses, and source trust hints
- linked source records and timestamps
- engine reasons and confidence
- active locks and prior review history
- whether the profile is high-value or high-risk

## Side-Effect Matrix

| Action | Review Action | Merge Event | Lock | Golden Profile Recompute | Downstream Event |
| --- | --- | --- | --- | --- | --- |
| assign | yes | no | no | no | no |
| unassign | yes | no | no | no | no |
| merge | yes | yes | no | yes | yes |
| reject | yes | optional | no | no | optional |
| manual_no_match | yes | yes | yes | no | optional |
| defer | yes | no | no | no | no |
| escalate | yes | no | no | no | optional |
| cancel | yes | no | no | no | no |

## Interaction With Unmerge

Unmerge should not be a normal reviewer action unless the reviewer also holds
admin privileges.

Recommended path:

1. reviewer identifies a likely bad prior merge
2. reviewer escalates with evidence
3. admin reviews merge history and impact
4. admin executes unmerge through admin flow
5. affected review cases are cancelled or recreated as needed

## Reopen Policy

Resolved or cancelled cases should not be reopened in place. Instead:

- create a new review case
- link the new case to prior case IDs in metadata
- preserve all prior actions for audit

This avoids mutating historical operational decisions.

## Reporting Metrics

Track at minimum:

- open, assigned, deferred, resolved, and cancelled counts
- average time to assign
- average time to resolve
- defer rate
- escalation rate
- manual no-match rate
- reviewer merge acceptance rate
- conflict rate from stale or concurrent actions

## Runbooks

At minimum, operations should have runbooks for:

- repeated false positives for a shared identifier
- incorrect reviewer merge requiring unmerge escalation
- review backlog breach
- source drift causing sudden review-volume spikes
- LLM disagreement rate spike if LLM assist is enabled

## Implementation Notes

- queue state and resolution should be stored separately
- every state transition should create a `review_action`
- merge and lock side effects should be transactionally tied to final review submission
- background recomputation should be idempotent if done asynchronously

## Recommendation

Implement the workflow in this order:

1. `open -> assigned -> resolved`
2. merge and reject actions
3. manual no-match locks
4. defer and SLA handling
5. escalation and admin handoff
6. unmerge escalation support

That sequence gets the core review operation live first while keeping room for
more advanced operational handling.
