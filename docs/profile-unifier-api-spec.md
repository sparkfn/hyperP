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

The API requires authenticated callers for every non-public endpoint. HyperP
accepts `Authorization: Bearer <token>` with either:

- Google ID tokens for human users authenticated through the browser UI.
- HyperP-issued OAuth2 client-credentials JWTs for machine callers.

Human authorization is role-based. Machine authorization is scope-based. OAuth
client access tokens carry a space-delimited `scope` claim and may include an
`entity_key` claim for entity-scoped integrations.

Recommended human roles:

- `support_agent`
- `reviewer`
- `admin`

Recommended machine scopes:

- `persons:read`: can read person and search endpoints.
- `persons:write`: can execute person write operations intended for machine callers.
- `ingest:write`: can call ingestion endpoints.
- `admin`: can manage admin-only resources and is a superset of all machine scopes.

Role and scope expectations:

- `support_agent`: can search and view person details with PII restrictions.
- `reviewer`: can access review queue and submit review actions.
- `admin`: can manual merge, unmerge, manage locks, and administer OAuth clients.
- OAuth clients must request only scopes assigned to the client; omitted token
  request scope defaults to the client's assigned scopes.

## Server-to-server authentication

Machine callers use OAuth2 client credentials. Admins create OAuth clients under
`/v1/admin/oauth-clients`, assign scopes, and receive a one-time `client_secret`.
Client secrets are not retrievable after creation or rotation.

Token request:

```http
POST /v1/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=hpc_...&client_secret=hps_...&scope=persons:read
```

Required form fields:

- `grant_type`: must be `client_credentials`.
- `client_id`: OAuth client identifier.
- `client_secret`: one active secret for the client.
- `scope`: optional space-delimited subset of the client's assigned scopes.

Successful token response:

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 900,
  "scope": "persons:read"
}
```

Successful token responses include `Cache-Control: no-store` and
`Pragma: no-cache` headers. Token endpoint errors are top-level OAuth JSON, not
the standard HyperP response envelope:

```json
{
  "error": "invalid_client",
  "error_description": "Invalid client credentials."
}
```

Supported token errors are `invalid_request`, `invalid_client`,
`invalid_scope`, and `unsupported_grant_type`.

Use the access token on API calls:

```http
Authorization: Bearer eyJ...
```

Public signing keys for HyperP-issued machine JWTs are available at
`GET /v1/oauth/jwks`.

## Admin OAuth client management

Admin users manage machine clients through `/v1/admin/oauth-clients`:

- `GET /v1/admin/oauth-clients`: list clients, including disabled clients and
  non-secret metadata for all secrets.
- `POST /v1/admin/oauth-clients`: create a client with assigned scopes and an
  optional `entity_key`; returns the initial `client_secret` once.
- `POST /v1/admin/oauth-clients/{client_id}/secrets`: rotate credentials by
  adding another active secret; returns the new `client_secret` once.
- `POST /v1/admin/oauth-clients/{client_id}/secrets/{secret_id}/revoke`: revoke
  one secret so it can no longer mint new access tokens.
- `POST /v1/admin/oauth-clients/{client_id}/disable`: disable the client so no
  secret for that client can mint new access tokens.
- `DELETE /v1/admin/oauth-clients/{client_id}`: permanently delete the client
  and all of its secrets.

Full client secrets are shown only in create and rotation responses. HyperP
stores hashed secret values and never returns a full secret from list or read
operations; those responses expose only metadata such as secret ID, prefix,
creation, expiry, revocation, and last-used timestamps. Rotation creates an
additional active secret so integrations can deploy the new credential before
revoking the old one. Secret revocation and client disabling stop future token
issuance but do not revoke already issued access tokens before their normal
expiry.

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
    "preferred_dob": "1989-10-01",
    "preferred_address": {
      "address_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
      "unit_number": null,
      "street_number": "10",
      "street_name": "Example Street",
      "city": "Singapore",
      "postal_code": "123456",
      "country_code": "SG",
      "normalized_full": "10 example street, singapore 123456, sg"
    }
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

Note: `preferred_address` is resolved from the `preferred_address_id` stored
on the Person node by traversing to the Address node at read time. The API
always returns the full structured address — consumers should never need to
resolve `address_id` themselves.

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
- rate limiting is enforced per caller role, with stricter limits for
  `support_agent`
- free-text `q` requires a minimum of 3 characters
- all search queries are logged with caller identity for audit

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

Supported `action_type` values for this endpoint:

- `merge`
- `reject`
- `defer`
- `escalate`
- `manual_no_match`

Note: The `review_action_type` enum also includes `assign`, `unassign`,
`cancel`, and `reopen`. These are system-recorded actions created internally by
the assign, unassign, and cancel endpoints rather than submitted through this
actions endpoint.

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
  "selected_source_record_pk": "35f78013-2347-4347-9226-4f94cbf6780d",
  "reason": "Customer manually confirmed preferred email."
}
```

Note: `selected_source_record_pk` identifies the SourceRecord whose `HAS_FACT`
relationship value should be preferred. In the graph model, attribute facts
are `HAS_FACT` relationships (not nodes), so the SourceRecord is the
addressable entity that pins the preferred value.

## Graph and Relationship APIs

## GET /v1/persons/{person_id}/connections

Return persons connected to the given person through shared identifiers
and/or shared addresses. This is the primary graph query for sales
(shared-identifier visibility), household detection (shared-address), and
contact tracing.

### Authorization

- `read_service`
- `support_agent`
- `reviewer`
- `admin`

### Query Parameters

- `connection_type`: optional, one of `identifier`, `address`, `all`.
  Default `all`. Controls which shared nodes to traverse.
- `identifier_type`: optional filter by identifier type (only applies when
  `connection_type` is `identifier` or `all`)
- `max_hops`: optional, default 1, max 3. Number of hops through shared
  nodes. 1 = direct shared identifier/address. 2+ = multi-hop contact
  tracing.
- `cursor`: optional pagination cursor
- `limit`: optional result size, default 20, max 100

### Response

```json
{
  "data": [
    {
      "person_id": "4aa7d8f2-d8ff-4db2-8d20-842111ab22ad",
      "status": "active",
      "preferred_full_name": "Bob Lee",
      "hops": 1,
      "shared_identifiers": [
        {
          "identifier_type": "phone",
          "normalized_value": "+6591234567"
        }
      ],
      "shared_addresses": [
        {
          "address_id": "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
          "normalized_full": "10 example street, singapore 123456, sg"
        }
      ]
    }
  ],
  "meta": {
    "request_id": "...",
    "next_cursor": null
  }
}
```

### Notes

- at `max_hops = 1`, this returns persons who directly share an identifier
  or address with the target — the most common sales use case
- at `max_hops > 1`, this performs multi-hop traversal for contact tracing;
  apply rate limiting and result caps to prevent runaway queries
- `shared_addresses` enables household detection: persons sharing the same
  normalized address are likely co-located
- support-agent role may receive redacted identifier values
- sensitive identifiers (government ID) should be excluded from the
  `shared_identifiers` response unless the caller has admin role
- `shared_addresses` and `shared_identifiers` arrays may both be populated
  for the same connected person if they share both

## GET /v1/persons/{person_id}/relationships

Return explicit typed relationships for a person (post-MVP). These are
declared relationships like `REFERRED_BY`, `WORKS_WITH`, `FAMILY_OF` — not
identity links.

### Authorization

- `read_service`
- `reviewer`
- `admin`

### Response

```json
{
  "data": [
    {
      "relationship_type": "REFERRED_BY",
      "direction": "outgoing",
      "related_person_id": "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
      "related_person_name": "Alice Tan",
      "source_system": "loyalty_app",
      "confidence": 1.0,
      "created_at": "2026-03-31T00:00:00Z"
    }
  ],
  "meta": {
    "request_id": "..."
  }
}
```

### Notes

- this endpoint is a placeholder for post-MVP; return empty array until
  explicit relationship types are implemented
- designing the endpoint now ensures downstream clients can integrate
  proactively

## Downstream Event APIs

## GET /v1/events

Poll for identity change events since a given timestamp.

### Authorization

- `read_service`
- `admin`

### Query Parameters

- `since`: required, ISO 8601 timestamp
- `event_type`: optional filter
- `cursor`: optional pagination cursor
- `limit`: optional result size, default 50, max 200

### Response

```json
{
  "data": [
    {
      "event_id": "a1b2c3d4-...",
      "event_type": "person_merged",
      "affected_person_ids": [
        "7af4b5f5-34c1-4f22-9e2d-95ea8ff3b8c7",
        "4aa7d8f2-d8ff-4db2-8d20-842111ab22ad"
      ],
      "metadata": {},
      "created_at": "2026-03-31T00:00:00Z"
    }
  ],
  "meta": {
    "request_id": "...",
    "next_cursor": "..."
  }
}
```

### Event Types

- `person_created`
- `person_merged`
- `person_unmerged`
- `golden_profile_updated`
- `review_case_resolved`
- `shared_identifier_detected` — emitted when a new `IDENTIFIED_BY`
  relationship connects a person to an Identifier node that already has other
  persons linked. Includes the identifier type and the set of affected person
  IDs. Enables downstream systems to react to newly discovered connections.
- `relationship_created` (post-MVP) — emitted when an explicit relationship
  is created between two persons

### Notes

- event schema is designed for future migration to a push-based delivery
  mechanism such as webhooks or message queues
- ordering must be stable by `created_at`

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

## Source Record Link Status Side Effects

When a review case is resolved or cancelled, the linked source record's
`link_status` must be updated:

- review resolved as `merge`: set `link_status = linked`
- review resolved as `reject` or `manual_no_match`: create a new person for
  the source record and set `link_status = linked`, or flag for manual triage
- review cancelled: re-run matching to find a new home, or flag for manual
  triage

Source records must not remain in `pending_review` after their review case is
no longer active.

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
3. `GET /persons/{person_id}/connections` (sales MVP — shared identifiers)
4. `GET /review-cases`
5. `POST /review-cases/{review_case_id}/actions`
6. `POST /persons/manual-merge`
7. `POST /persons/unmerge`

These endpoints unlock review operations, sales lookup, and downstream
consumption without forcing all ingestion or admin surfaces to be
production-complete on day one.
