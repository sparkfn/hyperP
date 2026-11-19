import type { NextResponse } from "next/server";

import type { BulkReviewActionResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<BulkReviewActionResponse>("/review-cases/bulk-actions", {
    method: "POST",
    body,
  });
}
