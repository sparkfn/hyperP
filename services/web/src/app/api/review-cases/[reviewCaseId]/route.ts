import type { NextResponse } from "next/server";

import type { ReviewCaseDetail } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ reviewCaseId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { reviewCaseId } = await context.params;
  return proxyToApi<ReviewCaseDetail>(
    `/review-cases/${encodeURIComponent(reviewCaseId)}`,
  );
}
