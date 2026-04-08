# @hyperp/web

Next.js 15 (App Router) frontend for HyperP. TypeScript strict, MUI v6.

## Architecture

Browser → Next.js Route Handlers (`/api/*`) → FastAPI service.

The browser never talks to FastAPI directly. All upstream calls go through
server-side route handlers in `src/app/api/`, which use the server-only client
in `src/lib/api-server.ts`. This keeps the API URL and any future credentials
out of the browser bundle.

## Setup

```bash
cd services/web
cp .env.local.example .env.local   # edit API_BASE_URL if needed
npm install
npm run dev                         # http://localhost:3001
```

Make sure the FastAPI service is running on `http://localhost:3000` (see the
root `docker-compose.yml`).

## Structure

```
src/
  app/
    layout.tsx                    # MUI theme + AppBar shell
    page.tsx                      # Person search (client component)
    persons/[personId]/page.tsx   # Person detail (server component)
    api/persons/                  # BFF route handlers → FastAPI
  lib/
    api-server.ts                 # server-only fetch wrapper
    api-types.ts                  # TS mirror of services/api/src/types.py
  theme.ts                        # MUI theme
```

## Notes

- API types in `src/lib/api-types.ts` are hand-maintained right now. When the
  contract stabilizes, replace with codegen from
  `docs/profile-unifier-openapi-3.1.yaml` (e.g. `openapi-typescript`).
- Port 3001 is used because the FastAPI service occupies 3000.
