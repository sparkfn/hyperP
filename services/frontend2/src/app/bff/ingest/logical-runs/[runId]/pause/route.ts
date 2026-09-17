import type { NextResponse } from "next/server";

import type { BoundedLogicalRunStatus } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ runId: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { runId } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<BoundedLogicalRunStatus>(
    `/ingest/logical-runs/${encodeURIComponent(runId)}/pause`,
    { method: "POST", body, signal: request.signal },
  );
}
