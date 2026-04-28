import type { NextResponse } from "next/server";

import type { PersonGraph } from "@/lib/api-types";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const { searchParams } = new URL(request.url);
  return proxyToApi<PersonGraph>(`/persons/${encodeURIComponent(personId)}/graph`, {
    query: searchParamsToQuery(searchParams),
  });
}
