import type { NextResponse } from "next/server";

import type { Profile } from "@/lib/api-types-person";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  return proxyToApi<Profile[]>("/profiles", {
    query: searchParamsToQuery(searchParams),
    signal: request.signal,
  });
}
