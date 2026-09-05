import type { NextResponse } from "next/server";

import type { PersonCrmDealMetrics } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(
  request: Request,
  context: RouteContext,
): Promise<NextResponse> {
  const { personId } = await context.params;
  return proxyToApi<PersonCrmDealMetrics>(
    `/persons/${encodeURIComponent(personId)}/crm/deal-metrics`,
    { signal: request.signal },
  );
}