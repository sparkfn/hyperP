import { NextResponse } from "next/server";

import type { IngestRunResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ sourceKey: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { sourceKey } = await context.params;
  const body: unknown = await request.json();
  const idempotencyKey: string | null = request.headers.get("Idempotency-Key");
  if (idempotencyKey === null || idempotencyKey.length === 0) {
    return NextResponse.json(
      { error: { code: "invalid_request", message: "Idempotency-Key is required." } },
      { status: 400 },
    );
  }
  return proxyToApi<IngestRunResponse>(
    `/ingest/${encodeURIComponent(sourceKey)}/runs`,
    { method: "POST", body, idempotencyKey },
  );
}
