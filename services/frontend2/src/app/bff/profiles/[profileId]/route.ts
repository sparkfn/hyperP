import type { NextResponse } from "next/server";

import type { ProfileDetail } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ profileId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<NextResponse> {
  const { profileId } = await context.params;
  return proxyToApi<ProfileDetail>(`/profiles/${encodeURIComponent(profileId)}`, {
    signal: request.signal,
  });
}
