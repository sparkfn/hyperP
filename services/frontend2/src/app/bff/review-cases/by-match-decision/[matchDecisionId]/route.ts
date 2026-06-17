import type { NextResponse } from "next/server";

import type { ReviewCaseDetail } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ matchDecisionId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { matchDecisionId } = await context.params;
  return proxyToApi<ReviewCaseDetail>(
    `/review-cases/by-match-decision/${encodeURIComponent(matchDecisionId)}`,
  );
}
