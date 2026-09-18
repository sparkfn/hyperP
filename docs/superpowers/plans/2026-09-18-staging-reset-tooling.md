# Plan: Staging Reset Tooling (Issue #441)

## 1. Intended Behavior and Architecture

A CLI module at `services/ingestion/src/staging_reset.py` that:

1. Refuses to run against production — checks environment variables **and** hostnames (system hostname, Neo4j URI host, Redis broker host) for production indicators.
2. Requires an explicit `--confirm` flag.
3. Clears all graph data nodes while preserving system configuration nodes.
4. Clears all ingestion-related Redis keys (watermarks, locks, semaphores, queue gates, caches, orchestration markers), including control-instance-scoped keys.
5. Logs the reset action with UTC ISO timestamps at start, per-phase progress, and completion.

Follows the established CLI pattern from `lifecycle_queue_admin.py`: `argparse`, integer exit codes, `main(argv=None) -> int`, `if __name__ == "__main__": raise SystemExit(main())`.

Cypher query constants live in `services/ingestion/src/graph/queries/staging_reset.py` per CLAUDE.md convention.

Invocation: `python -m src.staging_reset --confirm`

No subcommands — single-purpose tool.

---

## 2. Acceptance Criteria Mapping

| Acceptance Criterion | How Satisfied |
|---|---|
| Reset script clears graph data | Batched `DETACH DELETE` of all non-preserved nodes (§6) |
| Reset script clears Redis ingestion state | `SCAN profile_unifier:*` + `SCAN ci1:*` + `DELETE` + Celery queue purge (§7) |
| System configuration preserved | Explicit label exclusion list in Cypher `WHERE NOT` clause (§6) |
| Production safety guard (env var + hostname) | Multi-layer guard: env vars, system hostname, Neo4j URI host, Redis broker host (§5) |
| Requires `--confirm` | argparse flag, exit 1 if absent (§5) |
| Idempotent | Batched delete loop terminates when 0 remain; SCAN/DELETE on empty keyspace is a no-op |

---

## 3. Files to Create or Modify

| Action | Path | Purpose |
|---|---|---|
| **Create** | `services/ingestion/src/graph/queries/staging_reset.py` | Cypher query constant `STAGING_RESET_CLEAR_GRAPH` |
| **Modify** | `services/ingestion/src/graph/queries/__init__.py` | Import + re-export `STAGING_RESET_CLEAR_GRAPH` |
| **Create** | `services/ingestion/src/staging_reset.py` | CLI reset tool |
| **Create** | `services/ingestion/tests/test_staging_reset.py` | Unit tests |

---

## 4. Ordered Step-by-Step Changes

### Step 1: Create `services/ingestion/src/graph/queries/staging_reset.py`

Single module-level Cypher constant:

```python
"""Cypher query for staging graph reset."""

STAGING_RESET_CLEAR_GRAPH = (
    "MATCH (n) "
    "WHERE NOT n:Entity AND NOT n:SourceSystem "
    "AND NOT n:OAuthClient AND NOT n:OAuthClientSecret "
    "WITH n LIMIT $batch_size "
    "DETACH DELETE n "
    "RETURN count(*) AS deleted"
)
```

### Step 2: Update `services/ingestion/src/graph/queries/__init__.py`

Add import from `staging_reset` and add `STAGING_RESET_CLEAR_GRAPH` to `__all__`.

### Step 3: Create `services/ingestion/src/staging_reset.py`

Module structure (top to bottom):

```
Module docstring
Imports:
  stdlib: argparse, logging, os, socket, from datetime import datetime/timezone,
          from urllib.parse import urlsplit
  third-party: redis, from neo4j import ManagedTransaction
  local: from src.config import Settings, get_settings
         from src.graph.client import Neo4jClient
         from src.graph.queries.staging_reset import STAGING_RESET_CLEAR_GRAPH

logger = logging.getLogger(__name__)

Constants:
  _BLOCKED_ENV_VALUES  (frozenset: {"production", "prod"})
  _PRODUCTION_HOST_MARKERS (tuple: ("prod", "production"))
  _REDIS_SCAN_PATTERNS (tuple: ("profile_unifier:*", "ci1:*"))
  _CELERY_QUEUE_KEYS (tuple: ("ingestion", "lifecycle", "miscellaneous"))
  _BATCH_SIZE (10_000)

Functions:
  _utc_now_iso() -> str
  _host_looks_production(hostname: str) -> bool
  _check_environment_guard(settings: Settings) -> str | None
  _build_parser() -> argparse.ArgumentParser
  _delete_batch(tx: ManagedTransaction, batch_size: int) -> int
  _clear_graph(client: Neo4jClient, batch_size: int = _BATCH_SIZE) -> int
  _clear_redis(broker_url: str) -> int
  main(argv: list[str] | None = None) -> int

if __name__ == "__main__": raise SystemExit(main())
```

### Step 4: Create `services/ingestion/tests/test_staging_reset.py`

Unit tests covering guard logic (env vars + hostnames), CLI parsing, graph/Redis clearing, resource cleanup, and log output.

---

## 5. Production Safety Guard and Validation Logic

Multi-layer guard checking environment variables **and** hostnames. All checks run **before** any Neo4j or Redis connection is opened.

```python
_BLOCKED_ENV_VALUES: frozenset[str] = frozenset({"production", "prod"})
_PRODUCTION_HOST_MARKERS: tuple[str, ...] = ("prod", "production")


def _host_looks_production(hostname: str) -> bool:
    """Case-insensitive substring check for production indicators in a hostname."""
    lower = hostname.lower()
    return any(marker in lower for marker in _PRODUCTION_HOST_MARKERS)


def _check_environment_guard(settings: Settings) -> str | None:
    """Return an error message if any signal suggests production, else None."""
    # 1. Environment variables
    for env_value in (
        settings.deployment_environment,
        os.getenv("DEPLOYMENT_ENVIRONMENT", ""),
        os.getenv("ENVIRONMENT", ""),
    ):
        if env_value.lower() in _BLOCKED_ENV_VALUES:
            return (
                f"Refusing to reset: environment value {env_value!r} "
                f"indicates production."
            )

    # 2. System hostname
    for hostname in (socket.gethostname(), os.getenv("HOSTNAME", "")):
        if _host_looks_production(hostname):
            return (
                f"Refusing to reset: hostname {hostname!r} "
                f"contains a production indicator."
            )

    # 3. Service target hostnames (Neo4j URI, Redis broker URL)
    for label, uri in (
        ("neo4j_uri", settings.neo4j_uri),
        ("celery_broker_url", settings.celery_broker_url),
    ):
        parsed_host = urlsplit(uri).hostname or ""
        if _host_looks_production(parsed_host):
            return (
                f"Refusing to reset: {label} host {parsed_host!r} "
                f"contains a production indicator."
            )

    return None
```

In `main()`:
1. Load settings via `get_settings()`.
2. Call `_check_environment_guard(settings)`.
3. If error string returned → `logger.error(...)`, return exit code 1.
4. Check `--confirm` flag → if absent, `logger.error("Pass --confirm to proceed.")`, return exit code 1.
5. Only then proceed to graph/Redis clearing, with `try/finally` resource cleanup.

---

## 6. Graph Clearing Query and Preserved Nodes

### Preserved node labels (4 labels, per issue spec)

`Entity`, `SourceSystem`, `OAuthClient`, `OAuthClientSecret`.

Relationships **between** preserved nodes (e.g. `OPERATED_BY`, `HAS_SECRET`) survive because neither endpoint is deleted. Relationships **from** preserved nodes **to** deleted nodes are detached automatically by `DETACH DELETE`.

### Cypher constant (in `services/ingestion/src/graph/queries/staging_reset.py`)

```python
STAGING_RESET_CLEAR_GRAPH = (
    "MATCH (n) "
    "WHERE NOT n:Entity AND NOT n:SourceSystem "
    "AND NOT n:OAuthClient AND NOT n:OAuthClientSecret "
    "WITH n LIMIT $batch_size "
    "DETACH DELETE n "
    "RETURN count(*) AS deleted"
)
```

### Batched delete (in `staging_reset.py`)

The batch transaction callback is defined **outside** the loop to avoid ruff B023 (closure-in-loop capturing loop variable by reference):

```python
def _delete_batch(tx: ManagedTransaction, batch_size: int) -> int:
    """Single-batch graph deletion; called per transaction."""
    result = tx.run(STAGING_RESET_CLEAR_GRAPH, batch_size=batch_size)
    record = result.single()
    return int(record["deleted"]) if record else 0


def _clear_graph(client: Neo4jClient, batch_size: int = _BATCH_SIZE) -> int:
    total = 0
    while True:
        deleted = client.execute_write(
            lambda tx, _bs=batch_size: _delete_batch(tx, _bs),
        )
        total += deleted
        logger.info("Graph batch: deleted %d nodes (total so far: %d)", deleted, total)
        if deleted == 0:
            break
    return total
```

Each batch runs in its own transaction. Loop terminates when a batch deletes 0 nodes (idempotent). The `_bs=batch_size` default-arg binding in the lambda satisfies B023.

### What gets deleted

Every node label **not** in the preserved set, including but not limited to:
- Core data: `Person`, `Identifier`, `Address`, `SourceRecord`, `IngestRun`, `MatchDecision`, `ReviewCase`, `MergeEvent`, `Order`, `LineItem`, `Product`, `Vehicle`, `BankruptcyCase`, `ProfileAnalysis`
- Identity links: `IdentityLinkHead`, `IdentityLinkRevision`, `IdentityLinkRevisionCounter`
- Lifecycle/control: `SourceRecordIdentityLock`, `DataMigration`, `IngestionResetGeneration`, `IngestionLogicalRun`, `IngestionCheckpoint`
- Bounded ingestion: `BoundedIngestionScope`, `BoundedIngestionReceipt`, `BoundedIngestionRetry`, `BoundedIngestionGlobalSlot`, `BoundedUsageReservation`
- Scheduled: `ScheduledIngestionWorkflow`, `ScheduledMaintenanceObligation`, `ScheduledOccurrenceAuthority`, `ScheduledOccurrenceParticipant`
- All Bitrix CRM control/repair/history/tenant/company/census/stage nodes
- Sales staging: `StagedSalesOrder`, `StagedSalesLine`, `StagedSalesVehicleObservation`
- `BitrixSourceInstance` (re-bootstrapped on next ingestion startup)
- `Report`, `ProfileAnalysisRequest`, `GraphProbe`, `ResidualIdentifierReference`

**`DataMigration` nodes are deleted**, meaning schema migrations will re-run on next ingestion startup. All migrations use `IF NOT EXISTS` for constraints/indexes and `MERGE` for data, so re-running is safe and idempotent.

**`IngestionResetGeneration` nodes are deleted**, meaning bounded ingestion control starts fresh. A new active generation is created on next ingestion startup.

---

## 7. Redis State Keys Clearing Logic

All ingestion Redis keys live in db 0 (the Celery broker database). Two SCAN patterns are required:

1. **`profile_unifier:*`** — all standard ingestion keys.
2. **`ci1:*`** — control-instance-scoped keys generated by `scope_control_identity()` (format: `ci1:{len}:{control_instance_id}:{base}` where base contains `profile_unifier:...`).

API auth keys (`revoked:*`, `public_link:*`, `oauth_token:*`, `oauth_client_tokens:*`) do **not** match either pattern and are unaffected.

### Implementation

```python
_REDIS_SCAN_PATTERNS: tuple[str, ...] = ("profile_unifier:*", "ci1:*")
_CELERY_QUEUE_KEYS: tuple[str, ...] = ("ingestion", "lifecycle", "miscellaneous")


def _clear_redis(broker_url: str) -> int:
    client = redis.Redis.from_url(broker_url)
    try:
        count = 0
        for pattern in _REDIS_SCAN_PATTERNS:
            cursor: int = 0
            while True:
                cursor, keys = client.scan(cursor, match=pattern, count=1000)
                if keys:
                    client.delete(*keys)
                    count += len(keys)
                if cursor == 0:
                    break
        deleted_queues = client.delete(*_CELERY_QUEUE_KEYS)
        count += int(deleted_queues)
        return count
    finally:
        client.close()
```

### Keys cleared

**Via `profile_unifier:*` (26 key families):**

- Locks/Semaphores: `profile_unifier:ingestion:active`, `…:source:{source_key}[:{mode}][:{entity}]`, `…:init` + waiters, `…:bitrix-capability`, `…:source:bitrix_chat:crm_stage_history`
- Watermarks: `…:bitrix_openlines:{watermark,backfill}`, `…:whatsadmin-api:…`, `…:fundbox_api:…`, `…:phppos_api:…`
- Caches: `…:bitrix_openlines:dialog_config`
- Queue gates: `…:lifecycle-reconciliation:queued`, `…:knows-materialization:{phase}:queued`
- Orchestration: `…:ingestion:orchestration:{phase}:{id}`

**Via `ci1:*`:**

- Control-instance-scoped source locks and stage history locks (e.g. `ci1:5:abcde:profile_unifier:ingestion:source:...`).

**Celery queue keys:** `ingestion`, `lifecycle`, `miscellaneous` (stale pending tasks).

### What is NOT cleared

- Celery result backend (db 1) — stale results are harmless and self-expire.
- API auth keys — no matching prefix.
- Celery internal keys (e.g. `_kombu.*`, `unacked*`) — managed by the broker, not application state.

---

## 8. Resource Cleanup and Logging

### Deterministic resource cleanup

`main()` uses `try/finally` to ensure connections are closed:

```python
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = get_settings()

    guard_error = _check_environment_guard(settings)
    if guard_error is not None:
        logger.error(guard_error)
        return 1
    if not args.confirm:
        logger.error("Pass --confirm to proceed.")
        return 1

    logger.info(
        "[%s] Starting staging reset (neo4j=%s, redis=%s)",
        _utc_now_iso(),
        urlsplit(settings.neo4j_uri).hostname,
        urlsplit(settings.celery_broker_url).hostname,
    )

    graph_client = Neo4jClient(settings)
    try:
        nodes_deleted = _clear_graph(graph_client)
    finally:
        graph_client.close()

    redis_keys_deleted = _clear_redis(settings.celery_broker_url)
    # _clear_redis closes its own client in finally

    logger.info(
        "[%s] Staging reset complete: %d graph nodes deleted, %d Redis keys deleted.",
        _utc_now_iso(),
        nodes_deleted,
        redis_keys_deleted,
    )
    return 0
```

### Logging with UTC ISO timestamps

```python
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

Log messages emitted at:
- **Start**: target hosts (Neo4j hostname, Redis hostname) + UTC timestamp.
- **Per graph batch**: batch count and running total.
- **Per Redis scan pattern**: pattern and keys deleted.
- **Completion**: total nodes deleted, total Redis keys deleted, UTC timestamp.

All log lines use `logger.info()` or `logger.error()`. The `logging.basicConfig` call is in the `if __name__ == "__main__"` block (not in `main()`) so tests can inspect log output via `caplog` without side effects.

---

## 9. Testing Strategy

**File:** `services/ingestion/tests/test_staging_reset.py`

All tests are **pure unit tests** (no Neo4j, no Redis) using mocks, following the codebase pattern.

### Test cases

| Test | What it verifies |
|---|---|
| **Environment guard — env vars** | |
| `test_guard_refuses_production_env` | `deployment_environment="production"` → error string |
| `test_guard_refuses_prod_env` | `ENVIRONMENT=prod` (via `os.getenv`) → error string |
| `test_guard_refuses_deployment_env_override` | `DEPLOYMENT_ENVIRONMENT=production` (via `os.getenv`) → error string |
| `test_guard_allows_staging` | `deployment_environment="staging"` + no hostile env/host → `None` |
| `test_guard_allows_development` | `deployment_environment="development"` + no hostile env/host → `None` |
| **Environment guard — hostnames** | |
| `test_guard_refuses_production_system_hostname` | `socket.gethostname()` returns `"app-production-01"` → error string |
| `test_guard_refuses_hostname_env` | `HOSTNAME=prod-worker-3` → error string |
| `test_guard_refuses_neo4j_production_host` | `neo4j_uri="bolt://neo4j.prod.internal:7687"` → error string |
| `test_guard_refuses_redis_production_host` | `celery_broker_url="redis://redis.production.internal:6379/0"` → error string |
| **CLI parsing** | |
| `test_confirm_required` | `main([])` → exit code 1, no graph/redis calls |
| `test_main_production_exits` | `main(["--confirm"])` with production settings → exit code 1, no connections opened |
| **Graph clearing** | |
| `test_graph_clear_query_excludes_preserved` | Mock `execute_write`, verify Cypher text is `STAGING_RESET_CLEAR_GRAPH` with correct `WHERE NOT` exclusions |
| `test_graph_clear_batched_loop` | Mock returns 10000, 5000, 0 → total 15000, 3 calls, loop exits |
| `test_graph_clear_empty` | Mock returns 0 on first call → total 0, 1 call (idempotent) |
| **Redis clearing** | |
| `test_redis_scans_both_patterns` | Mock `redis.Redis`, verify `scan` called with both `"profile_unifier:*"` and `"ci1:*"` |
| `test_redis_deletes_celery_queues` | Mock `redis.Redis`, verify `delete` called with `("ingestion", "lifecycle", "miscellaneous")` |
| `test_redis_client_closed` | Verify `client.close()` called even if scan raises |
| **Resource cleanup** | |
| `test_graph_client_closed_on_error` | Mock `execute_write` to raise, verify `client.close()` still called |
| **Logging** | |
| `test_main_success_logs_timestamps` | `main(["--confirm"])` with staging settings → exit 0, `caplog` contains UTC ISO timestamps at start and completion |

### Mocking approach

- Patch `src.staging_reset.get_settings` to return a mock `Settings` with controlled `deployment_environment`, `neo4j_uri`, `celery_broker_url`.
- Patch `src.staging_reset.Neo4jClient` to avoid real connections; mock `execute_write` and `close`.
- Patch `redis.Redis.from_url` to return a mock Redis client with `scan`/`delete`/`close`.
- Patch `socket.gethostname` and `os.getenv` for hostname guard tests.
- Use `unittest.mock.patch` for all external dependencies.
- Use `pytest.caplog` (via `caplog` fixture) to assert timestamped log messages.

---

## 10. Risks, Assumptions, and Open Questions

### Assumptions

1. **Ingestion workers are stopped** before running the reset. The script does not stop workers or check for in-flight tasks. This is documented in the CLI help text.
2. **Schema re-initialization** (constraints/indexes) and **Entity/SourceSystem re-bootstrap** happen automatically on next ingestion startup — the reset tool does not run them.
3. **Celery result backend (db 1)** does not need clearing — stale results self-expire via TTL and are harmless.
4. **`BitrixSourceInstance`** is deleted and re-bootstrapped on next startup via `bootstrap_legacy_bitrix_source_instance()`.

### Risks

1. **Batch size tuning.** 10,000 is a reasonable default for staging data volumes. If staging accumulates millions of nodes, individual batches may still be slow. Mitigation: the batch size is a constant that can be adjusted.
2. **Concurrent access.** If any service reads the graph or Redis during the reset, it may see partial state. Mitigation: documented precondition that workers must be stopped first.

### Open Questions

1. **`--dry-run` flag.** Should the tool support a dry-run mode that reports what would be cleared without actually clearing? Not in the issue scope — defer to follow-up if needed.
