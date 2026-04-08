import type { NextResponse } from "next/server";

import type { ReviewAssignResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ reviewCaseId: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { reviewCaseId } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<ReviewAssignResponse>(
    `/review-cases/${encodeURIComponent(reviewCaseId)}/assign`,
    { method: "POST", body },
  );
}
