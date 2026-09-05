import type { NextResponse } from "next/server";

import type { PersonCrmActivityMetrics } from "@/lib/api-types";
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
  return proxyToApi<PersonCrmActivityMetrics>(
    `/persons/${encodeURIComponent(personId)}/crm/activity-metrics`,
    { signal: request.signal },
  );
}