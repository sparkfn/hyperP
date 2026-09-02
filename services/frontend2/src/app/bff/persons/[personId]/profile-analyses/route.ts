import type { NextResponse } from "next/server";

import type { PersonProfileAnalyses, ProfileAnalysisRequestResult } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  return proxyToApi<PersonProfileAnalyses>(
    `/persons/${encodeURIComponent(personId)}/profile-analyses`,
    { signal: request.signal },
  );
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<ProfileAnalysisRequestResult>(
    `/persons/${encodeURIComponent(personId)}/profile-analyses/requests`,
    { method: "POST", body, signal: request.signal },
  );
}
