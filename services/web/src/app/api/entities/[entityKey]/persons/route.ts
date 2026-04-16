import type { NextResponse } from "next/server";

import type { EntityPerson } from "@/lib/api-types";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ entityKey: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { entityKey } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<EntityPerson[]>(`/entities/${encodeURIComponent(entityKey)}/persons`, {
    query: searchParamsToQuery(searchParams),
  });
}
