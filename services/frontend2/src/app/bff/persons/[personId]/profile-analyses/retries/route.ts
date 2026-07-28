import type { NextResponse } from "next/server";

import type { ProfileAnalysisRetryResult } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<ProfileAnalysisRetryResult>(
    `/persons/${encodeURIComponent(personId)}/profile-analyses/retries`,
    { method: "POST", body },
  );
}
