# Dump File Ingestion Source Design

## Purpose

Add database dump files as an ingestion source option for production environments where direct database connectivity is not yet available. Users can start an ingestion run for any source system either through the existing direct path or by selecting a dump file from a mounted dumps directory.

## Goals

- Keep the existing source-system ingestion entry point.
- Let users choose direct ingestion or database dump ingestion every time they start a run.
- Mount host `./dumps` into the relevant containers.
- List files recursively from the mounted dumps directory.
- Allow users to select a listed file or type a relative dump path manually.
- Store ingestion `mode` and optional `dump_path` on run records.
- Show `mode` and `dump_path` in run history/details.
- Preserve existing API-run creation behavior; no scheduler or worker execution changes in this scope.

## Non-goals

- No dump file format validation.
- No SQL parsing or dump import execution.
- No direct database connection implementation.
- No Celery enqueueing from the UI/API start action.
- No ingestion monitoring UI changes.
- No per-source capability gating; dump ingestion is available for all source systems.

## Backend API

Extend the existing ingestion run creation path instead of adding a separate dump-specific endpoint.

`POST /v1/ingest/{source_key}/runs` accepts:

- `mode`: ingestion mode, defaulting to `batch` for backward compatibility.
- `dump_path`: optional relative path under the configured dumps root.

Supported initial modes:

- `batch`: existing direct ingestion metadata flow.
- `dump`: dump-file ingestion metadata flow.

For `mode = dump`, `dump_path` is required. For `mode = batch`, `dump_path` is omitted.

The API validates path shape at the request boundary:

- reject absolute paths;
- reject `..` traversal;
- reject empty dump paths for dump mode;
- normalize separators to a consistent relative path representation.

The API does not check whether the selected file exists or is readable when creating the run. Missing or unreadable files will be represented later as failed ingestion runs by worker-side dump execution logic.

## Data Model

Store the following properties on `IngestRun` records:

- `mode`: `batch` or `dump`.
- `dump_path`: relative path string, present only for dump runs.

Run list and detail responses include both fields so the frontend can display them in history and detail views. Existing callers that do not send `mode` continue to create `batch` runs.

## Dumps Directory Listing

Add a read-only API endpoint that lists files under the configured dumps root recursively. The endpoint returns relative file paths, for example:

```text
fundbox/2026-05-01.sql
pos/archive/customer_dump.sql
```

The endpoint:

- lists files only, not directories as selectable entries;
- includes files in subdirectories;
- does not validate extensions or inspect contents;
- never returns absolute container paths;
- returns a controlled API error if the dumps root is missing, not mounted, or unreadable.

Use a single backend configuration value such as `DUMPS_ROOT=/app/dumps`. Docker Compose mounts host `./dumps` to that path. Mount the same path into the worker for parity, even though worker-side execution is outside this scope.

## Frontend Flow

Keep the existing “Run ingestion” action, but make it open a choice dialog every time.

The dialog has two paths:

1. **Direct ingestion**
   - Creates a run with `mode = batch`.
   - Sends no `dump_path`.

2. **Database dump file**
   - Shows the recursive dump-file list from the backend.
   - Allows the user to select a listed file.
   - Also allows manual relative path entry.
   - Creates a run with `mode = dump` and the selected or typed `dump_path`.

Frontend validation is only for immediate user feedback:

- dump path is required for dump mode;
- absolute paths are rejected;
- `..` traversal is rejected.

The frontend does not require manually typed files to appear in the backend listing.

Run history/details display the ingestion mode and, for dump runs, the dump path.

## BFF/API Boundary

The browser continues to call Next.js BFF route handlers rather than FastAPI directly. The existing run-creation BFF forwards `mode` and `dump_path` to the API. A new BFF route proxies the dumps listing endpoint.

The BFF should keep handlers thin and reuse the existing `proxyToApi` pattern.

## Error Handling

- Invalid path shape returns a request validation error before creating the run.
- Missing or unreadable dumps root returns a controlled API error from the listing endpoint.
- Missing or unreadable selected files are not checked during run creation.
- Future worker-side dump ingestion will mark runs failed if the dump file cannot be opened or processed.

## Testing

Backend tests cover:

- existing run creation remains backward-compatible and defaults to `batch`;
- dump run creation stores and returns `mode` and `dump_path`;
- dump mode requires `dump_path`;
- absolute paths and traversal paths are rejected;
- recursive dumps listing returns relative file paths;
- missing or unreadable dumps root returns a controlled error.

Frontend/type-check coverage should include:

- dialog switching between direct and dump modes;
- dump list rendering;
- manual path validation;
- correct BFF payloads for batch versus dump modes;
- display of mode and dump path in run history/details.

## Implementation Boundaries

This feature is metadata and selection plumbing only. It prepares the product surface and run records for dump-based ingestion while leaving ingestion execution, scheduling, and monitoring to separate work.
