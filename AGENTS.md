# AGENTS.md

## Purpose and precedence

This file is the operating guide for Codex agents working in HyperP. It translates
the repository's established guidance into Codex-specific workflow rules. Follow
this file and the user's request; consult `CLAUDE.md` for full architectural
context and examples. If guidance conflicts, use the stricter safety, Git, or CI
constraint.

HyperP unifies customer profiles and relationship intelligence across POS, Bitrix
CRM, and third-party systems. Primary code is in `services/api/` and
`services/ingestion/`; product and architecture documents are in `docs/`.

## Codex workflow

- Use PowerShell for shell commands in this Windows workspace.
- Start by inspecting relevant files and `git status -sb`; preserve unrelated
  worktree changes.
- Use `rg` / `rg --files` for searches. Use `apply_patch` for file edits.
- Do not create local status, plan, or breadcrumb Markdown files. GitHub issues,
  PRs, branches, commits, and CI results are the source of truth.
- Before handoff, perform a hostile review: correctness, edge cases, security,
  brittle tests, duplication, and contract compatibility.
- Never expose secrets, tokens, private keys, or credentials.

## Commands and validation

Codex has full access to the local project toolchain and Docker runtime. It may run
package, test, lint, type-check, build, migration, application-server, and Docker
Compose commands when they are relevant to the user's request. This includes
`uv run`, `pytest`, `mypy`, `ruff`, `npm run`, `npx eslint`, local virtual
environments, and short-lived or explicitly requested long-lived services.

Use the smallest command scope that provides useful evidence. Preserve existing
host state and avoid leaving unnecessary processes, containers, generated files,
or dependency artifacts behind. Before destructive operations such as deleting
data, resetting databases, removing containers or volumes, or applying irreversible
migrations, identify the exact target and obtain explicit user confirmation.

Local validation may include both structural checks and project commands, for
example:

```powershell
git status -sb
git diff --check
git diff
rg --files
uv run pytest
npm run lint
docker compose ps
```

Local checks provide fast feedback, but Woodpecker remains the required verifier
for pushed PR and post-merge `main` work.

Validate through Woodpecker after an authorized push to a PR branch:

```powershell
wpci home doctor --json
wpci home repo ls
wpci home pipeline last sparkfn/hyperP --branch <branch>
wpci home pipeline show sparkfn/hyperP <pipeline-number>
wpci home pipeline log show sparkfn/hyperP <pipeline-number> <step-name>
```

After an authorized PR merge into `main`, inspect the post-merge pipeline with:

```powershell
wpci home pipeline ls sparkfn/hyperP --branch main
wpci home pipeline show sparkfn/hyperP <pipeline-number>
```

Because pull-request pipelines can also report `branch: main`, select the latest
pipeline whose `event` is `push`; do not assume the first branch match is MAIN CI.

Inspect Woodpecker **only** with `wpci home`; never open its UI, use tokens, or
use legacy wrappers. Do not claim work complete without required pipeline evidence:
repository/branch or PR, commit SHA, pipeline number, status, and step names.
Missing, skipped, or failed checks block completion unless the user explicitly
accepts a partial result with a tracked follow-up.

## Git and CI discipline

- Never commit, stage, push, merge, or open a PR without explicit user approval.
- Never push directly to `main`. Do not push to `development` without explicit
  authorization.
- `development` is the integration branch. New PRs default to the branch their
  PR branch was based on; do not assume `development` is the target branch.
  `staging` triggers the existing GitHub Actions deployment and must not be
  changed casually.
- When creating a branch or worktree, base it on the current `HEAD`, never
  `main` or `origin/main`. Recreate any mistakenly main-based worktree before use.
- Do not weaken, skip, rename, or remove checks to make CI pass.
- Do not recreate host-local, GitHub-runner, or git-runner validation workflows.
- If root `docker-compose.yml` changes, make the equivalent change in
  `.docker/staging/docker-compose.yml` in the same commit.

Woodpecker is untrusted. Canonical workflows are `.woodpecker/pr.yaml` for pull
requests and `.woodpecker/main.yaml` for pushes to `main`. PR CI runs Python
lint/type/test checks plus frontend2 typecheck and ESLint errors-only. MAIN reruns
those checks on the merged commit and adds the frontend production build and frozen
production Python install. Do not add Docker sockets, `volumes:`, or
`docker compose up` to PR/MAIN workflows.

## Active application and topology

- `services/frontend2/` is the only active frontend and is served at `/`.
  `services/frontend/` is retired reference code: do not add code or run commands
  there.
- Services are Neo4j, Redis, FastAPI (`api`), Next.js (`frontend2`), nginx (`web`),
  and Celery worker/beat. The API is internal; nginx provides public routing.
- The active authenticated UI API contract is the FastAPI mount `/app/v2`.
  Add/remove mounted routes in the appropriate app builder, not root `src/app.py`.
- Browser code must use Next.js BFF handlers under `src/app/bff/`; it must never
  call FastAPI directly. Public pages use `/api/v1/public/...` with no bearer token.
- Keep `NEXT_PUBLIC_BASE_PATH`, Next config, middleware, nginx locations, and API
  mount behavior distinct and aligned when changing route topology.
- Every schema-visible FastAPI endpoint must be exposed through MCP. Add new routers to
  `src.route_catalog`, verify each operation resolves to a stable unique operation ID, and extend the
  API-to-MCP parity test. Exclusions require an explicit documented security or transport reason.

## Backend rules

- Routes depend on repository `Protocol`s through `Depends`; routes must not call
  `get_session()` or import `src.graph.*` directly.
- Put Neo4j implementations in `repositories/neo4j/`, protocols in
  `repositories/protocols/`, and singleton dependency wiring in `repositories/deps.py`.
- Put Cypher in `graph/queries/` constants or builder functions. Parameterize
  values; never interpolate them into query strings.
- Return `ApiResponse[T]` through `envelope()` unless the endpoint is an existing
  documented bare admin/machine response. Preserve cursor pagination conventions.
- Use existing graph converters and mappers. Cypher boolean projections are Python
  `bool`; timezone-aware Neo4j datetimes must be normalized before arithmetic.
- Format human-facing dates and percentages in API display helpers, not in the UI.
- Public endpoints belong on a router included without auth dependencies; the
  action that creates public data remains authenticated.
- Dispatch ingestion through Celery (`run_ingestion_task.delay`), never by calling
  `run_ingestion()` directly. `limited-100` dumps are local/development only.
- `sgbankruptcy` and `sgrentalflats` are dump-only; always give them dump mode and
  a path relative to `DUMPS_ROOT`.

## Python standards

- Target strict typing and `mypy --strict`: explicit concrete variables,
  parameters, attributes, and return types; no `Any`, loose containers, or broad
  type escape hatches. Use `TypedDict`, `Protocol`, dataclasses, models, and unions.
- Keep modules roughly under 400 lines and functions under 50 lines; extract
  cohesive helpers and query modules instead of growing monoliths.
- Use Pydantic request/response models and typed FastAPI parameters/dependencies.
- Use `uv` for dependency changes (`uv add` / `uv remove`), never `pip`, Poetry,
  requirements files, or Pipenv.
- End Python files with a newline; keep non-query lines at 100 characters or less;
  bind every closure-captured loop variable as a default argument.
- Avoid redundant casts and validate untrusted data at the boundary rather than
  scattering casts downstream.

## Frontend standards

- TypeScript is strict: no `any`, `as any`, or `as unknown as T`. Validate external
  data with guards or schemas before narrowing.
- Give exported functions, components, route handlers, and Server Actions explicit
  return types. Prefer discriminated string unions to `enum`.
- Model API payloads with the existing typed interfaces; do not use
  `Record<string, unknown>` as an escape hatch.
- Server-only modules import `server-only`; client components begin with
  `"use client"` and never import server-only code or secrets.
- Next 15 route handlers use typed async params and are thin proxies/services.
  Prefer Server Components for read-only data and parallel fetches where appropriate.
- Keep components roughly under 150 lines and modules under 300 lines. Use
  per-component MUI imports, `@/` aliases, and existing theme/shared UI patterns.
- Use `DatePickerField` for date ranges, with ISO values and `DD MMM YYYY` display.
- Do not remove Dockerfiles' `npm install --legacy-peer-deps` requirement.

The local lint budget (`npm run lint`, max 9 warnings) is currently exceeded by
about 18 pre-existing warnings. CI intentionally uses `npx eslint src` errors-only;
ensure a change adds no warnings and do not suppress `react-hooks/set-state-in-effect`
for a callback-only effect.

## Documentation and domain constraints

- Name new design documents `profile-unifier-*.md`; use glossary terminology and
  update the README document map/reading order when adding documents.
- Keep API prose and `docs/profile-unifier-openapi-3.1.yaml` consistent. Use Mermaid
  for sequence diagrams.
- Preserve core product decisions: precision over recall, immutable source facts,
  explainable merge decisions, controlled LLM rollout, protected sensitive IDs,
  and repository-mediated graph access.
