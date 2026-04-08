import type { NextResponse } from "next/server";

import type { Person } from "@/lib/api-types";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  return proxyToApi<Person[]>("/persons/search", { query: searchParamsToQuery(searchParams) });
}
