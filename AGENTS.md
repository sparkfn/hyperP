# AGENTS.md — agent operating policy for sparkfn/hyperP

This file is the durable agent-policy counterpart to `CLAUDE.md`. Coding standards,
service topology, and codebase patterns live in `CLAUDE.md`; this file owns the
**workflow, CI, and Git discipline** every agent must follow here.

## Source of truth
GitHub issues, PRs, branches, commits, and CI results are the source of truth. Do not
create local `.md` status/plan/breadcrumb files for task history — record decisions,
CI maps, evidence, and follow-ups in the relevant issue or PR.

## Branch model
- `main` — production lineage (do not push directly).
- `development` — integration branch. PRs merge here. DEV CI runs on push to this branch.
- `staging` — staging deploy target (GitHub Actions `.github/workflows/deploy-staging.yml`).
- Feature/fix work happens on worktrees/branches off the current branch/HEAD, **not** `origin/main`.

## CI — hybrid (Woodpecker + GitHub Actions)
- **PR + DEV validation** run in local **Woodpecker** (`corbu` host, docker backend,
  `https://ci.corbu.dev`). Inspect **only** via `wpci home`. Never open the Woodpecker
  UI, paste tokens, or use legacy wrapper scripts.
- **Staging deploy** runs in GitHub Actions on push to `staging`. Leave it unchanged.

### Workflows (canonical layout)
| Boundary | File | `when:` | Branch |
|---|---|---|---|
| PR | `.woodpecker/pr.yaml` | `event: pull_request` | any |
| DEV | `.woodpecker/dev.yaml` | `event: push` + `branch: development` | `development` |

PR is fast feedback only (ruff, mypy --strict, pytest, frontend2 typecheck + eslint
errors-only). DEV is materially stronger (same python checks on the merge commit +
frontend2 production `next build` + production `uv sync --frozen --no-dev`). The repo
is **untrusted** in Woodpecker — no `volumes:`, no Docker socket, no `docker compose up`
in PR/DEV workflows; those belong to MAIN/staging deploy only. Never weaken, skip, or
rename tests/checks to make CI pass.

## Agent hard rules
1. **No project package/test/build/migration/app-server commands on the host** — no
   `uv run pytest`, `npm run build`, `npm test`, `venv`, migrations, or long-lived
   processes. Validate by pushing to a PR branch and reading the Woodpecker result via
   `wpci home`.
2. Agents may inspect/edit files, run Git, and run safe structural checks
   (`git diff --check`, `git status -sb`).
3. **Commit discipline**: never commit, push, or merge without explicit user confirmation.
   This overrides any plan step that says "commit".
4. **Do not push to `development`** without explicit user authorization — DEV CI only
   runs after an authorized merge/push.
5. **Completion gates** — do not report work complete without pipeline evidence:
   - PR: `wpci home pipeline show sparkfn/hyperP <n>` — repo, branch/PR, commit SHA,
     pipeline number, status, step names.
   - DEV: a `development`-branch pipeline number, status, commit SHA, and step names.
6. Missing, skipped, or failing PR/DEV checks are blockers unless the user explicitly
   accepts partial/blocked adoption with a recorded follow-up issue.
7. Do not recreate git-runner / GitHub runner / host-local dependency/test workflows.
8. Never print secrets or tokens. Woodpecker secrets belong in Woodpecker, not YAML.

## Frontend lint gate
`npm run lint` (`--max-warnings 9`) is red on a clean tree (~18 pre-existing warnings, 0
errors); PR/DEV CI run `npx eslint src` (errors only). Verify changes add zero net warnings
(stash and compare). Full detail in CLAUDE.md (*Frontend lint gate*); keep both in sync.

## Inspecting CI
Inspect Woodpecker **only** via `wpci home` — never the UI, tokens, or legacy wrappers.
Commands and the CI-only-empty-commit retrigger path are documented in CLAUDE.md
(*Inspecting CI*); keep both in sync.

## docker-compose sync
Any commit modifying the root `docker-compose.yml` must apply the equivalent change to
`.docker/staging/docker-compose.yml` in the same commit. Full rule in CLAUDE.md
(*docker-compose.yml sync rule*); keep both in sync.