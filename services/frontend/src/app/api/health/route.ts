import { NextResponse } from "next/server";

import type { HealthResponse } from "@/lib/api-types-ops";

export const dynamic = "force-dynamic";

// The upstream FastAPI health endpoint lives at `/health`, NOT under `/v1`.
// `apiFetch` / `proxyToApi` always prepend `API_BASE_URL`, which already
// includes the `/v1` suffix (see lib/api-server.ts). Rather than mutating the
// shared server module, we derive the upstream origin here by stripping the
// trailing `/v1` (if present) from `API_BASE_URL` and calling `fetch` directly.
// We intentionally do NOT reuse `apiFetch` because it hard-codes the `/v1`
// prefix via the base URL.
function healthUrl(): string {
  const base: string = process.env.API_BASE_URL ?? "http://localhost:3000/v1";
  const origin: string = base.replace(/\/v1\/?$/, "");
  return `${origin}/health`;
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const v: Record<string, unknown> = value as Record<string, unknown>;
  return (
    typeof v.status === "string" &&
    typeof v.neo4j === "string" &&
    typeof v.timestamp === "string"
  );
}

export async function GET(): Promise<NextResponse> {
  try {
    const response: Response = await fetch(healthUrl(), {
      method: "GET",
      headers: { accept: "application/json" },
      cache: "no-store",
    });
    const text: string = await response.text();
    const parsed: unknown = text.length > 0 ? JSON.parse(text) : null;
    const body: HealthResponse = isHealthResponse(parsed)
      ? parsed
      : {
          status: response.ok ? "ok" : "degraded",
          neo4j: "unknown",
          timestamp: new Date().toISOString(),
          error: response.ok ? null : response.statusText,
        };
    return NextResponse.json({ data: body }, { status: response.status });
  } catch (err: unknown) {
    const message: string = err instanceof Error ? err.message : "Health check failed.";
    const body: HealthResponse = {
      status: "degraded",
      neo4j: "disconnected",
      timestamp: new Date().toISOString(),
      error: message,
    };
    return NextResponse.json({ data: body }, { status: 503 });
  }
}
