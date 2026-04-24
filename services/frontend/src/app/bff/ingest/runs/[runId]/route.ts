import type { NextResponse } from "next/server";

import type { IngestRunDetailResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ runId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { runId } = await context.params;
  return proxyToApi<IngestRunDetailResponse>(`/ingest/runs/${encodeURIComponent(runId)}`);
}
