import type { NextResponse } from "next/server";

import type { ReviewCaseSummary } from "@/lib/api-types-ops";
import { proxyToApi, searchParamsToQuery } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  return proxyToApi<ReviewCaseSummary[]>("/review-cases", {
    query: searchParamsToQuery(searchParams),
  });
}
