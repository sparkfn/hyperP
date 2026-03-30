# Profile Unifier API Spec

## Purpose

Define a concrete HTTP API contract for the profile unifier platform. This
spec is intended for backend implementation, internal client integration, and
review-tool development.

## API Style

- protocol: HTTPS
- payload format: JSON
- versioning: path versioning with `/v1`
- time format: ISO 8601 UTC timestamps
- identifiers: UUIDs for internal entities, strings for source-owned IDs

Base path:

```text
/v1
```

## Design Principles

- write APIs should be idempotent where retries are expected
- read APIs should expose lineage and audit context where relevant
- search APIs should support operational lookup first, analytics second
- review and merge actions must be explicit and auditable
- sensitive identifier handling must be role-aware

## Authentication and Authorization

The API should require authenticated callers. Recommended auth choices:

- service-to-service JWT or mTLS for system integrations
- session or JWT-based auth for internal reviewer tools

Recommended roles:

- `ingest_service`
- `read_service`
- `service`
- `support_agent`
- `reviewer`
- `admin`

Role expectations:

- `ingest_service`: can call ingestion endpoints only
- `read_service`: can read person and search endpoints
- `service`: can execute internal platform operations such as recomputation jobs
- `support_agent`: can search and view person details with PII restrictions
- `reviewer`: can access review queue and submit review actions
- `admin`: can manual merge, unmerge, and manage locks

## Standard Headers

Recommended headers:

- `Authorization: Bearer <token>`
- `Content-Type: application/json`
- `Idempotency-Key: <uuid>` for retryable write operations
- `X-Request-Id: <uuid>` for tracing

## Standard Response Envelope

Successful responses should use:

```json
{
  "data": {},
  "meta": {
    "request_id": "6a7d748a-8ff7-49c1-8c74-0a8ef09a2a4f"
  }
}
```

Paginated responses should use:

```json
{
  "data": [],
  "meta": {
    "request_id": "6a7d748a-8ff7-49c1-8c74-0a8ef09a2a4f",
    "next_cursor": "eyJvZmZzZXQiOjEwMH0="
  }
}
```

Error responses should use:

```json
{
  "error": {
    "code": "review_case_not_found",
    "message": "Review case was not found.",
    "details": {}
  },
  "meta": {
    "request_id": "6a7d748a-8ff7-49c1-8c74-0a8ef09a2a4f"
  }
}
```

## Error Codes

Recommended standard error codes:

- `invalid_request`
- `unauthorized`
- `forbidden`
- `not_found`
- `conflict`
- `rate_limited`
- `unprocessable_entity`
- `review_case_not_found`
- `person_not_found`
- `manual_lock_conflict`
- `merge_blocked`

## Resource Models

## Person Summary

```json
{
  "person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
  "status": "active",
  "is_high_value": false,
  "is_high_risk": false,
  "golden_profile": {
    "preferred_full_name": "Alice Tan",
    "preferred_phone": "+6591234567",
    "preferred_email": "alice@example.com",
    "preferred_dob": "1989-10-01"
  },
  "source_record_count": 4,
  "identifier_counts": {
    "phone": 2,
    "email": 3
  },
  "created_at": "2026-03-31T00:00:00Z",
  "updated_at": "2026-03-31T00:00:00Z"
}
```

## Source Record

```json
{
  "source_record_pk": "8d6efb3f-97b4-42e2-8ccb-325fa8d9a77f",
  "source_system": "bitrix",
  "source_record_id": "12345",
  "source_record_version": "2026-03-30T10:00:00Z",
  "link_status": "linked",
  "linked_person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
  "observed_at": "2026-03-30T10:00:00Z",
  "ingested_at": "2026-03-31T00:00:00Z"
}
```

## Match Decision

```json
{
  "match_decision_id": "f3ee6977-846e-421e-baa4-6fe2b7ee33aa",
  "engine_type": "heuristic",
  "engine_version": "v1.0.0",
  "policy_version": "policy-2026-03-31",
  "decision": "review",
  "confidence": 0.78,
  "reasons": [
    "same phone",
    "same DOB",
    "highly similar name"
  ],
  "blocking_conflicts": []
}
```

## Review Case

```json
{
  "review_case_id": "8d0d1fa0-f5e5-47e6-85e5-c37aa6a4f6b6",
  "queue_state": "open",
  "priority": 100,
  "assigned_to": null,
  "follow_up_at": null,
  "sla_due_at": "2026-04-01T00:00:00Z",
  "match_decision": {}
}
```

## Ingestion APIs

## POST /v1/ingest/{source_key}/records

Submit one or more normalized source records for ingestion.

### Authorization

- `ingest_service`

### Request

```json
{
  "ingest_type": "batch",
  "ingest_run_id": "optional-existing-run-id",
  "records": [
    {
      "source_record_id": "12345",
      "source_record_version": "2026-03-30T10:00:00Z",
      "observed_at": "2026-03-30T10:00:00Z",
      "record_hash": "sha256:abc123",
      "identifiers": [
        {
          "type": "phone",
          "value": "+6591234567",
          "is_verified": false
        },
        {
          "type": "email",
          "value": "alice@example.com",
          "is_verified": false
        }
      ],
      "attributes": {
        "full_name": "Alice Tan",
        "dob": "1989-10-01",
        "address": "10 Example Street"
      },
      "raw_payload": {}
    }
  ]
}
```

### Response

```json
{
  "data": {
    "accepted_count": 1,
    "rejected_count": 0,
    "ingest_run_id": "e2ac40fb-f508-44e8-8df1-1d23d40d4a75",
    "results": [
      {
        "source_record_id": "12345",
        "status": "accepted"
      }
    ]
  },
  "meta": {
    "request_id": "..."
  }
}
```

### Notes

- treat `Idempotency-Key` as required for retries
- batch size should be capped by implementation policy
- response should not expose raw PII beyond operational need

## POST /v1/ingest/{source_key}/runs

Create an ingest run before a bulk sync.

### Authorization

- `ingest_service`

### Request

```json
{
  "run_type": "historical_backfill",
  "metadata": {
    "trigger": "manual"
  }
}
```

### Response

```json
{
  "data": {
    "ingest_run_id": "e2ac40fb-f508-44e8-8df1-1d23d40d4a75",
    "status": "started",
    "started_at": "2026-03-31T00:00:00Z"
  },
  "meta": {
    "request_id": "..."
  }
}
```

## PATCH /v1/ingest/{source_key}/runs/{ingest_run_id}

Update run status after completion or failure.

### Authorization

- `ingest_service`

### Request

```json
{
  "status": "completed",
  "finished_at": "2026-03-31T01:00:00Z",
  "metadata": {
    "accepted_count": 1000,
    "rejected_count": 12
  }
}
```

### Response

```json
{
  "data": {
    "ingest_run_id": "e2ac40fb-f508-44e8-8df1-1d23d40d4a75",
    "status": "completed",
    "finished_at": "2026-03-31T01:00:00Z"
  },
  "meta": {
    "request_id": "..."
  }
}
```

## Search and Person Read APIs

## GET /v1/persons/search

Operational search for canonical persons.

### Authorization

- `read_service`
- `support_agent`
- `reviewer`
- `admin`

### Query Parameters

- `identifier_type`: optional, one of `phone`, `email`, `external_customer_id`, `membership_id`, `crm_contact_id`, `government_id_hash`
- `value`: optional exact search value
- `q`: optional free-text search for name or display fields
- `source_system`: optional source filter
- `status`: optional person status filter
- `cursor`: optional pagination cursor
- `limit`: optional result size, default 20, max 100

At least one of `identifier_type + value` or `q` should be provided.

### Response

```json
{
  "data": [
    {
      "person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
      "status": "active",
      "golden_profile": {
        "preferred_full_name": "Alice Tan",
        "preferred_phone": "+6591234567",
        "preferred_email": "alice@example.com"
      },
      "source_record_count": 4
    }
  ],
  "meta": {
    "request_id": "...",
    "next_cursor": null
  }
}
```

### Notes

- support-agent role may receive redacted sensitive fields
- government-ID-derived lookups should be exact-match only

## GET /v1/persons/{person_id}

Return the full canonical person view.

### Authorization

- `read_service`
- `support_agent`
- `reviewer`
- `admin`

### Query Parameters

- `include`: comma-separated optional expansions

Supported expansions:

- `identifiers`
- `attributes`
- `source_records`
- `audit`
- `locks`

### Response

```json
{
  "data": {
    "person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
    "status": "active",
    "is_high_value": false,
    "is_high_risk": false,
    "golden_profile": {
      "preferred_full_name": "Alice Tan",
      "preferred_phone": "+6591234567",
      "preferred_email": "alice@example.com",
      "preferred_dob": "1989-10-01"
    },
    "identifiers": [],
    "attributes": [],
    "source_records": [],
    "locks": []
  },
  "meta": {
    "request_id": "..."
  }
}
```

## GET /v1/persons/{person_id}/source-records

Return source records linked to the person.

### Authorization

- `read_service`
- `support_agent`
- `reviewer`
- `admin`

## GET /v1/persons/{person_id}/audit

Return merge, unmerge, review, and override history for the person.

### Authorization

- `reviewer`
- `admin`

## GET /v1/persons/{person_id}/matches

Return recent match decisions related to the person.

### Authorization

- `reviewer`
- `admin`

## Review Queue APIs

## GET /v1/review-cases

List review cases.

### Authorization

- `reviewer`
- `admin`

### Query Parameters

- `queue_state`
- `assigned_to`
- `follow_up_before`
- `priority_lte`
- `source_system`
- `cursor`
- `limit`

### Response

```json
{
  "data": [
    {
      "review_case_id": "8d0d1fa0-f5e5-47e6-85e5-c37aa6a4f6b6",
      "queue_state": "open",
      "priority": 100,
      "follow_up_at": null,
      "sla_due_at": "2026-04-01T00:00:00Z",
      "match_decision": {
        "match_decision_id": "f3ee6977-846e-421e-baa4-6fe2b7ee33aa",
        "engine_type": "heuristic",
        "decision": "review",
        "confidence": 0.78
      }
    }
  ],
  "meta": {
    "request_id": "...",
    "next_cursor": null
  }
}
```

## GET /v1/review-cases/{review_case_id}

Return a single review case with its candidate comparison payload.

### Authorization

- `reviewer`
- `admin`

### Response

```json
{
  "data": {
    "review_case_id": "8d0d1fa0-f5e5-47e6-85e5-c37aa6a4f6b6",
    "queue_state": "assigned",
    "priority": 100,
    "assigned_to": "reviewer_123",
    "follow_up_at": null,
    "sla_due_at": "2026-04-01T00:00:00Z",
    "match_decision": {
      "match_decision_id": "f3ee6977-846e-421e-baa4-6fe2b7ee33aa",
      "engine_type": "heuristic",
      "decision": "review",
      "confidence": 0.78,
      "reasons": [
        "same phone",
        "same DOB"
      ]
    },
    "comparison": {
      "left_entity": {},
      "right_entity": {}
    }
  },
  "meta": {
    "request_id": "..."
  }
}
```

## POST /v1/review-cases/{review_case_id}/assign

Assign a review case.

### Authorization

- `reviewer`
- `admin`

### Request

```json
{
  "assigned_to": "reviewer_123"
}
```

### Notes

- repeated assignment to the same owner should be treated as idempotent
- assignment should fail with `409 Conflict` if optimistic concurrency checks fail

## POST /v1/review-cases/{review_case_id}/unassign

Release ownership and return the case to the queue.

### Authorization

- `reviewer`
- `admin`

### Response

```json
{
  "data": {
    "review_case_id": "8d0d1fa0-f5e5-47e6-85e5-c37aa6a4f6b6",
    "queue_state": "open",
    "assigned_to": null
  },
  "meta": {
    "request_id": "..."
  }
}
```

### Notes

- repeated unassignment of an already-open case should be treated as idempotent
- unassignment should fail with `409 Conflict` on stale ownership writes

## POST /v1/review-cases/{review_case_id}/actions

Submit a review action.

### Authorization

- `reviewer`
- `admin`

### Request

```json
{
  "action_type": "merge",
  "notes": "Phone and DOB align. Name variant is acceptable.",
  "metadata": {
    "create_manual_lock": false,
    "follow_up_at": null,
    "escalation_reason": null
  }
}
```

Supported `action_type` values:

- `merge`
- `reject`
- `defer`
- `escalate`
- `manual_no_match`

### Response

```json
{
  "data": {
    "review_case_id": "8d0d1fa0-f5e5-47e6-85e5-c37aa6a4f6b6",
    "queue_state": "resolved",
    "resolution": "merge"
  },
  "meta": {
    "request_id": "..."
  }
}
```

## Merge and Lock APIs

## POST /v1/persons/manual-merge

Manually merge two canonical persons.

### Authorization

- `admin`

### Request

```json
{
  "from_person_id": "4aa7d8f2-d8ff-4db2-8d20-842111ab22ad",
  "to_person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
  "reason": "Confirmed duplicate by identity steward.",
  "recompute_golden_profile": true
}
```

### Response

```json
{
  "data": {
    "merge_event_id": "3bb15b4e-b017-4c38-a5e7-6e8d514f7f6f",
    "from_person_id": "4aa7d8f2-d8ff-4db2-8d20-842111ab22ad",
    "to_person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
    "status": "completed"
  },
  "meta": {
    "request_id": "..."
  }
}
```

## POST /v1/persons/unmerge

Undo a prior merge event and restore separated lineage.

### Authorization

- `admin`

### Request

```json
{
  "merge_event_id": "3bb15b4e-b017-4c38-a5e7-6e8d514f7f6f",
  "reason": "Merge was incorrect after review."
}
```

## POST /v1/locks/person-pair

Create a persistent lock to prevent repeated suggestions.

### Authorization

- `reviewer`
- `admin`

### Request

```json
{
  "left_person_id": "4aa7d8f2-d8ff-4db2-8d20-842111ab22ad",
  "right_person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
  "lock_type": "manual_no_match",
  "reason": "Shared business phone, not the same individual.",
  "expires_at": null
}
```

## DELETE /v1/locks/{lock_id}

Remove an existing lock.

### Authorization

- `admin`

## Golden Profile and Survivorship APIs

## POST /v1/persons/{person_id}/golden-profile/recompute

Trigger recomputation of a person's golden profile.

### Authorization

- `service`
- `admin`

## POST /v1/persons/{person_id}/survivorship-overrides

Create or replace a field-level survivorship override.

### Authorization

- `reviewer`
- `admin`

### Request

```json
{
  "attribute_name": "preferred_email",
  "selected_person_attribute_fact_id": "35f78013-2347-4347-9226-4f94cbf6780d",
  "reason": "Customer manually confirmed preferred email."
}
```

## Operational and Admin APIs

## GET /v1/ingest/runs/{ingest_run_id}

Return ingest run status and counters.

### Authorization

- `ingest_service`
- `admin`

## GET /v1/source-systems

List configured source systems and status.

### Authorization

- `admin`

## GET /v1/source-systems/{source_key}/field-trust

Return field-level trust configuration.

### Authorization

- `admin`

## PATCH /v1/source-systems/{source_key}/field-trust

Update trust tiers for one or more fields.

### Authorization

- `admin`

## API Behavior Rules

## Idempotency

The following endpoints should require `Idempotency-Key`:

- `POST /v1/ingest/{source_key}/records`
- `POST /v1/ingest/{source_key}/runs`
- `POST /v1/review-cases/{review_case_id}/assign`
- `POST /v1/review-cases/{review_case_id}/unassign`
- `POST /v1/review-cases/{review_case_id}/actions`
- `POST /v1/persons/manual-merge`
- `POST /v1/persons/unmerge`
- `POST /v1/locks/person-pair`
- `POST /v1/persons/{person_id}/golden-profile/recompute`
- `POST /v1/persons/{person_id}/survivorship-overrides`

## Pagination

- use cursor-based pagination
- return `next_cursor` only when another page exists
- ordering must be stable for review and audit endpoints

## Redaction Rules

- support-agent responses may redact government-ID-derived identifiers
- reviewer responses may expose more evidence than support-agent responses
- raw payloads should not be returned from standard person endpoints
- dedicated admin or forensic endpoints can be added later if needed

## Merge Safety Rules

- `manual-merge` must fail with `merge_blocked` if a hard no-match lock exists
- `unmerge` must fail with `conflict` if referenced merge event is invalid
- review action `merge` should create an audit event and close the review case
- review action `manual_no_match` should create a persistent lock

## Suggested HTTP Status Codes

- `200 OK` for successful reads and updates
- `201 Created` for explicit creates where useful
- `202 Accepted` if async processing is introduced later
- `400 Bad Request` for invalid payloads
- `401 Unauthorized` for missing auth
- `403 Forbidden` for insufficient role
- `404 Not Found` for missing resources
- `409 Conflict` for lock or state conflicts
- `422 Unprocessable Entity` for valid JSON with invalid business state

## OpenAPI Recommendation

This document should be translated into an OpenAPI 3.1 contract once the
implementation stack is chosen. The OpenAPI spec should preserve:

- shared response envelopes
- role annotations
- idempotency requirements
- redaction notes for sensitive fields

## Recommendation

Implement read and workflow APIs first:

1. `GET /persons/search`
2. `GET /persons/{person_id}`
3. `GET /review-cases`
4. `POST /review-cases/{review_case_id}/actions`
5. `POST /persons/manual-merge`
6. `POST /persons/unmerge`

These endpoints unlock review operations and downstream consumption without
forcing all ingestion or admin surfaces to be production-complete on day one.
