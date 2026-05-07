# Dump File Ingestion Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add metadata-only dump file ingestion selection so users can create ingestion runs with `mode="dump"` and a relative dump path from mounted `./dumps`.

**Architecture:** Extend the existing ingestion run creation API rather than adding a separate dump-run endpoint. Add a read-only dumps listing route, store `mode` and `dump_path` on `IngestRun`, proxy both through the BFF, and update the existing start-run dialog and run detail page.

**Tech Stack:** FastAPI, Pydantic v2, Neo4j Cypher, Next.js App Router, TypeScript, MUI, Docker Compose, uv/pytest, npm/tsc/eslint.

---

## File Structure

- Modify `services/api/src/types_requests.py`: add `IngestRunMode`, `mode`, `dump_path`, and path-shape validation to `IngestRunCreateRequest`.
- Modify `services/api/src/repositories/protocols/ingest.py`: return `mode` and `dump_path` in run response dataclasses; pass mode/path into `create_run`.
- Modify `services/api/src/graph/queries/ingestion.py`: persist and return `mode` and `dump_path` in run Cypher.
- Modify `services/api/src/repositories/neo4j/ingest.py`: pass mode/path into Cypher and map returned fields.
- Create `services/api/src/routes/dumps.py`: list files recursively under `config.dumps_root` using relative POSIX paths.
- Modify `services/api/src/config.py`: add `dumps_root` setting.
- Modify `services/api/src/app.py`: register the dumps router behind active auth.
- Create `services/api/tests/test_ingest_dump_runs.py`: test request validation and dump listing helper behavior.
- Modify `services/api/tests/test_oauth_routes.py`: update fake ingest repo signatures/dataclasses for new fields.
- Modify `services/frontend/src/lib/api-types-ops.ts`: mirror new `mode`, `dump_path`, and dump listing types.
- Create `services/frontend/src/app/bff/dumps/route.ts`: proxy dumps listing endpoint.
- Modify `services/frontend/src/components/StartIngestionRunDialog.tsx`: add direct-vs-dump dialog flow, list loading, manual path validation, and payload changes.
- Modify `services/frontend/src/app/ingestion/runs/[runId]/page.tsx`: display `mode` and `dump_path`.
- Modify `docker-compose.yml`: add `DUMPS_ROOT` and `./dumps:/app/dumps:ro` for API and worker.

## Task 1: Backend run metadata and validation

**Files:**
- Modify: `services/api/src/types_requests.py`
- Modify: `services/api/src/repositories/protocols/ingest.py`
- Modify: `services/api/src/graph/queries/ingestion.py`
- Modify: `services/api/src/repositories/neo4j/ingest.py`
- Modify: `services/api/tests/test_oauth_routes.py`
- Test: `services/api/tests/test_ingest_dump_runs.py`

- [ ] **Step 1: Write failing request validation tests**

Create `services/api/tests/test_ingest_dump_runs.py` with:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.types_requests import IngestRunCreateRequest


def test_ingest_run_create_defaults_to_batch_mode() -> None:
    body = IngestRunCreateRequest(run_type="manual")

    assert body.mode == "batch"
    assert body.dump_path is None


def test_dump_run_requires_dump_path() -> None:
    with pytest.raises(ValidationError, match="dump_path is required"):
        IngestRunCreateRequest(run_type="manual", mode="dump")


def test_dump_run_rejects_absolute_dump_path() -> None:
    with pytest.raises(ValidationError, match="relative to the dumps root"):
        IngestRunCreateRequest(run_type="manual", mode="dump", dump_path="/tmp/file.sql")


def test_dump_run_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError, match="must not contain parent traversal"):
        IngestRunCreateRequest(run_type="manual", mode="dump", dump_path="../file.sql")


def test_dump_run_normalizes_backslashes() -> None:
    body = IngestRunCreateRequest(
        run_type="manual",
        mode="dump",
        dump_path="fundbox\\archive\\dump.sql",
    )

    assert body.dump_path == "fundbox/archive/dump.sql"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/api/tests/test_ingest_dump_runs.py -q`

Expected: FAIL because `IngestRunCreateRequest` has no `mode`/`dump_path` behavior yet.

- [ ] **Step 3: Implement request model validation**

In `services/api/src/types_requests.py`, update imports and `IngestRunCreateRequest`:

```python
from pathlib import PurePosixPath
from typing import Literal
```

```python
IngestRunMode = Literal["batch", "dump"]
```

```python
class IngestRunCreateRequest(BaseModel):
    run_type: str
    mode: IngestRunMode = "batch"
    dump_path: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_dump_path(self) -> IngestRunCreateRequest:
        if self.mode == "batch":
            if self.dump_path is not None:
                raise ValueError("dump_path is only valid when mode='dump'")
            return self
        if self.dump_path is None or self.dump_path.strip() == "":
            raise ValueError("dump_path is required when mode='dump'")
        normalized = self.dump_path.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if path.is_absolute():
            raise ValueError("dump_path must be relative to the dumps root")
        if ".." in path.parts:
            raise ValueError("dump_path must not contain parent traversal")
        self.dump_path = path.as_posix()
        return self
```

- [ ] **Step 4: Run request validation tests to verify they pass**

Run: `uv run pytest services/api/tests/test_ingest_dump_runs.py -q`

Expected: PASS.

- [ ] **Step 5: Extend repository protocol dataclasses and signature**

In `services/api/src/repositories/protocols/ingest.py`, update dataclasses:

```python
@dataclass
class IngestRunResponse:
    ingest_run_id: str
    status: str
    mode: str
    dump_path: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
```

```python
@dataclass
class IngestRunDetailResponse:
    ingest_run_id: str
    run_type: str
    mode: str
    dump_path: str | None
    status: str
    record_count: int
    rejected_count: int
    started_at: str | None
    finished_at: str | None
    source_key: str | None
```

Update `create_run` signature:

```python
    async def create_run(
        self,
        source_key: str,
        run_type: str,
        mode: str,
        dump_path: str | None,
        metadata: dict[str, str],
    ) -> IngestRunResponse | None:
        """Returns None if the source system is not found or inactive."""
        ...
```

- [ ] **Step 6: Update route to pass mode/path**

In `services/api/src/routes/ingest.py`, change create call to:

```python
    result = await repo.create_run(
        source_key,
        body.run_type,
        body.mode,
        body.dump_path,
        body.metadata,
    )
```

- [ ] **Step 7: Update Cypher to persist and return mode/path**

In `services/api/src/graph/queries/ingestion.py`, update `CREATE_INGEST_RUN` properties:

```cypher
  run_type: $run_type,
  mode: $mode,
  dump_path: $dump_path,
  status: 'started',
```

Update return:

```cypher
RETURN ir.ingest_run_id AS ingest_run_id,
       ir.status AS status,
       ir.mode AS mode,
       ir.dump_path AS dump_path,
       toString(ir.started_at) AS started_at
```

Update `GET_INGEST_RUN` projection:

```cypher
RETURN ir {
  .ingest_run_id, .run_type, .mode, .dump_path, .status,
  .record_count, .rejected_count, .metadata
} AS run,
```

- [ ] **Step 8: Update Neo4j repository mapping**

In `services/api/src/repositories/neo4j/ingest.py`, update `create_run` and `_create_run_tx` signatures to include `mode: str` and `dump_path: str | None`, pass them to `CREATE_INGEST_RUN`, and return:

```python
    return IngestRunResponse(
        ingest_run_id=to_str(record["ingest_run_id"]),
        status=to_str(record["status"]),
        mode=to_str(record["mode"]),
        dump_path=to_str(record["dump_path"]) or None,
        started_at=to_str(record["started_at"]),
    )
```

Update `get_run` return:

```python
        return IngestRunDetailResponse(
            ingest_run_id=to_str(run.get("ingest_run_id")),
            run_type=to_str(run.get("run_type")),
            mode=to_str(run.get("mode")) or "batch",
            dump_path=to_str(run.get("dump_path")) or None,
            status=to_str(run.get("status")),
            record_count=int(run.get("record_count") or 0),
            rejected_count=int(run.get("rejected_count") or 0),
            started_at=to_str(record["started_at"]) or None,
            finished_at=to_str(record["finished_at"]) or None,
            source_key=to_str(record["source_key"]) or None,
        )
```

- [ ] **Step 9: Update OAuth route fake repository**

In `services/api/tests/test_oauth_routes.py`, update fake `create_run` signature and dataclass construction to include `mode` and `dump_path`. Existing `IngestRunResponse(...)` calls should include `mode="batch"`. Existing `IngestRunDetailResponse(...)` calls should include `mode="batch", dump_path=None`.

- [ ] **Step 10: Run backend tests for ingestion routes**

Run: `uv run pytest services/api/tests/test_ingest_dump_runs.py services/api/tests/test_oauth_routes.py -q`

Expected: PASS.

## Task 2: Backend dumps listing endpoint

**Files:**
- Modify: `services/api/src/config.py`
- Create: `services/api/src/routes/dumps.py`
- Modify: `services/api/src/app.py`
- Test: `services/api/tests/test_ingest_dump_runs.py`

- [ ] **Step 1: Add failing dumps listing tests**

Append to `services/api/tests/test_ingest_dump_runs.py`:

```python
from pathlib import Path

from src.routes.dumps import list_dump_files


def test_list_dump_files_returns_recursive_relative_posix_paths(tmp_path: Path) -> None:
    (tmp_path / "fundbox" / "archive").mkdir(parents=True)
    (tmp_path / "fundbox" / "archive" / "dump.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "root.sql").write_text("select 2", encoding="utf-8")

    files = list_dump_files(tmp_path)

    assert files == ["fundbox/archive/dump.sql", "root.sql"]


def test_list_dump_files_rejects_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        list_dump_files(missing)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest services/api/tests/test_ingest_dump_runs.py -q`

Expected: FAIL because `src.routes.dumps` does not exist.

- [ ] **Step 3: Add dumps root config**

In `services/api/src/config.py`, add to `AppConfig`:

```python
    dumps_root: str = Field(default="/app/dumps", alias="DUMPS_ROOT")
```

- [ ] **Step 4: Create dumps route module**

Create `services/api/src/routes/dumps.py`:

```python
"""Dump-file discovery endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_scope
from src.config import config
from src.http_utils import envelope, http_error
from src.types import ApiResponse

router = APIRouter()


def list_dump_files(root: Path) -> list[str]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(str(root))
    files = [
        path.relative_to(resolved).as_posix()
        for path in resolved.rglob("*")
        if path.is_file()
    ]
    return sorted(files)


@router.get(
    "/v1/dumps",
    response_model=ApiResponse[list[str]],
    dependencies=[Depends(require_scope("ingest:write"))],
)
async def list_dumps(request: Request) -> ApiResponse[list[str]]:
    try:
        files = list_dump_files(Path(config.dumps_root))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        raise http_error(
            503,
            "dumps_root_unavailable",
            "Dumps directory is not mounted or readable.",
            request,
        ) from None
    return envelope(files, request)
```

- [ ] **Step 5: Register dumps router**

In `services/api/src/app.py`, import `dumps` in the `src.routes` import list and add:

```python
    app.include_router(dumps.router, dependencies=active)
```

near the ingest router.

- [ ] **Step 6: Run dumps tests**

Run: `uv run pytest services/api/tests/test_ingest_dump_runs.py -q`

Expected: PASS.

## Task 3: Frontend BFF, types, and dialog flow

**Files:**
- Modify: `services/frontend/src/lib/api-types-ops.ts`
- Create: `services/frontend/src/app/bff/dumps/route.ts`
- Modify: `services/frontend/src/components/StartIngestionRunDialog.tsx`
- Modify: `services/frontend/src/app/ingestion/runs/[runId]/page.tsx`

- [ ] **Step 1: Update frontend API types**

In `services/frontend/src/lib/api-types-ops.ts`, add:

```typescript
export type IngestRunMode = "batch" | "dump";
```

Update `IngestRunCreateRequest`:

```typescript
export interface IngestRunCreateRequest {
  run_type: string;
  mode: IngestRunMode;
  dump_path: string | null;
  metadata: Record<string, string>;
}
```

Update `IngestRunResponse`:

```typescript
export interface IngestRunResponse {
  ingest_run_id: string;
  status: string;
  mode: IngestRunMode;
  dump_path: string | null;
  started_at: string | null;
  finished_at: string | null;
}
```

Update `IngestRunDetailResponse`:

```typescript
export interface IngestRunDetailResponse {
  ingest_run_id: string;
  run_type: string;
  mode: IngestRunMode;
  dump_path: string | null;
  status: string;
  record_count: number;
  rejected_count: number;
  started_at: string | null;
  finished_at: string | null;
  source_key: string | null;
}
```

- [ ] **Step 2: Add BFF dumps route**

Create `services/frontend/src/app/bff/dumps/route.ts`:

```typescript
import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  return proxyToApi<string[]>("/dumps");
}
```

- [ ] **Step 3: Add dialog helper types and validation**

In `services/frontend/src/components/StartIngestionRunDialog.tsx`, import `Radio`, `RadioGroup`, `FormControlLabel`, `FormLabel`, `CircularProgress`, and update types:

```typescript
type IngestionModeChoice = "batch" | "dump";

type RunType = "manual" | "scheduled" | "backfill";

function normalizeDumpPath(value: string): string {
  return value.trim().replaceAll("\\", "/");
}

function validateDumpPath(value: string): string | null {
  const normalized = normalizeDumpPath(value);
  if (normalized.length === 0) return "Dump path is required.";
  if (normalized.startsWith("/")) return "Dump path must be relative to ./dumps.";
  if (normalized.split("/").includes("..")) {
    return "Dump path must not contain parent traversal.";
  }
  return null;
}
```

- [ ] **Step 4: Update dialog state and open behavior**

Add state:

```typescript
  const [mode, setMode] = useState<IngestionModeChoice>("batch");
  const [dumpFiles, setDumpFiles] = useState<string[]>([]);
  const [dumpPath, setDumpPath] = useState<string>("");
  const [loadingDumps, setLoadingDumps] = useState<boolean>(false);
```

Update `handleOpen` to reset those fields and call `void loadDumpFiles();`.

Add:

```typescript
  async function loadDumpFiles(): Promise<void> {
    setLoadingDumps(true);
    try {
      const files: string[] = await bffFetch<string[]>("/bff/dumps");
      setDumpFiles(files);
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to load dump files.";
      setError(message);
    } finally {
      setLoadingDumps(false);
    }
  }
```

- [ ] **Step 5: Update submit payload**

In `handleSubmit`, before setting submitting, add:

```typescript
    const normalizedDumpPath: string | null = mode === "dump" ? normalizeDumpPath(dumpPath) : null;
    if (mode === "dump") {
      const dumpPathError = validateDumpPath(dumpPath);
      if (dumpPathError !== null) {
        setError(dumpPathError);
        return;
      }
    }
```

Update request body to:

```typescript
          body: JSON.stringify({
            run_type: runType,
            mode,
            dump_path: normalizedDumpPath,
            metadata: parsedMetadata,
          }),
```

- [ ] **Step 6: Update dialog UI**

Inside the `<Stack>`, above run type, add a radio group:

```tsx
            <FormLabel>Ingestion source</FormLabel>
            <RadioGroup
              value={mode}
              onChange={(e) => setMode(e.target.value === "dump" ? "dump" : "batch")}
            >
              <FormControlLabel value="batch" control={<Radio />} label="Direct ingestion" />
              <FormControlLabel value="dump" control={<Radio />} label="Database dump file" />
            </RadioGroup>
```

After run type, add dump controls:

```tsx
            {mode === "dump" ? (
              <Stack spacing={1}>
                <TextField
                  select
                  label="Choose dump file"
                  value={dumpFiles.includes(dumpPath) ? dumpPath : ""}
                  onChange={(e) => setDumpPath(e.target.value)}
                  size="small"
                  disabled={loadingDumps}
                  helperText="Files are listed recursively from ./dumps."
                >
                  {dumpFiles.map((file) => (
                    <MenuItem key={file} value={file}>
                      {file}
                    </MenuItem>
                  ))}
                </TextField>
                {loadingDumps ? (
                  <Stack direction="row" spacing={1} alignItems="center">
                    <CircularProgress size={16} />
                    <Typography variant="caption" color="text.secondary">
                      Loading dump files...
                    </Typography>
                  </Stack>
                ) : null}
                <TextField
                  label="Manual dump path"
                  value={dumpPath}
                  onChange={(e) => setDumpPath(e.target.value)}
                  size="small"
                  helperText="Relative to ./dumps, for example fundbox/archive/dump.sql."
                />
              </Stack>
            ) : null}
```

- [ ] **Step 7: Show mode/path after creation**

In success alert, add:

```tsx
                  Mode: <strong>{created.mode}</strong>
                  {created.dump_path !== null ? <> · Dump: <strong>{created.dump_path}</strong></> : null}
```

- [ ] **Step 8: Update run detail page**

In `services/frontend/src/app/ingestion/runs/[runId]/page.tsx`, update metadata:

```typescript
    mode: run.mode,
    dump_path: run.dump_path,
```

Update chips:

```tsx
            <Chip label={run.mode} variant="outlined" />
            {run.dump_path !== null ? <Chip label={run.dump_path} variant="outlined" /> : null}
```

- [ ] **Step 9: Run frontend typecheck**

Run: `npm --prefix services/frontend run typecheck`

Expected: PASS.

## Task 4: Docker Compose dumps mount

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add API dump config and mount**

In `docker-compose.yml`, add `DUMPS_ROOT: ${DUMPS_ROOT:-/app/dumps}` to the API service environment and add API volumes:

```yaml
    volumes:
      - ./dumps:/app/dumps:ro
```

- [ ] **Step 2: Add worker dump mount for parity**

In the worker service, add:

```yaml
    volumes:
      - ./dumps:/app/dumps:ro
```

- [ ] **Step 3: Check compose config parses**

Run: `docker compose config --quiet`

Expected: exit code 0.

## Task 5: Full verification

**Files:**
- All modified files

- [ ] **Step 1: Run API lint and focused tests**

Run:

```bash
uv run --package profile-unifier-api ruff check services/api/src services/api/tests/test_ingest_dump_runs.py services/api/tests/test_oauth_routes.py
uv run pytest services/api/tests/test_ingest_dump_runs.py services/api/tests/test_oauth_routes.py -q
```

Expected: PASS.

- [ ] **Step 2: Run API type check**

Run: `uv run --package profile-unifier-api mypy --strict services/api/src`

Expected: PASS or only documented pre-existing strict failures from `types_sales.py` / `types_requests.py` if unchanged by this feature.

- [ ] **Step 3: Run frontend checks**

Run:

```bash
npm --prefix services/frontend run typecheck
npm --prefix services/frontend run lint
```

Expected: PASS within existing ESLint warning budget.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short
git diff --stat
git diff -- docs/superpowers/specs/2026-05-07-dump-file-ingestion-source-design.md docs/superpowers/plans/2026-05-07-dump-file-ingestion-source.md services/api/src services/api/tests services/frontend/src docker-compose.yml
```

Expected: only intended files changed.
