import type { NextResponse } from "next/server";

import type { FieldTrustResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ sourceKey: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { sourceKey } = await context.params;
  return proxyToApi<FieldTrustResponse>(
    `/source-systems/${encodeURIComponent(sourceKey)}/field-trust`,
  );
}

export async function PATCH(request: Request, context: RouteContext): Promise<NextResponse> {
  const { sourceKey } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<FieldTrustResponse>(
    `/source-systems/${encodeURIComponent(sourceKey)}/field-trust`,
    { method: "PATCH", body },
  );
}
