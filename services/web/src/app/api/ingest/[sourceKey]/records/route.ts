import type { NextResponse } from "next/server";

import type { IngestRecordsResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ sourceKey: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { sourceKey } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<IngestRecordsResponse>(
    `/ingest/${encodeURIComponent(sourceKey)}/records`,
    { method: "POST", body },
  );
}
