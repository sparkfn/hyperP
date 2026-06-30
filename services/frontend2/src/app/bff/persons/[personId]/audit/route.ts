import type { NextResponse } from "next/server";

import type { PersonAuditEvent } from "@/lib/api-types-person";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<PersonAuditEvent[]>(
    `/persons/${encodeURIComponent(personId)}/audit`,
    { query: searchParamsToQuery(searchParams) },
  );
}
