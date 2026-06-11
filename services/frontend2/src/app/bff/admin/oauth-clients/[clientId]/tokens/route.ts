import type { NextResponse } from "next/server";
import type { OAuthAccessToken } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ clientId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<OAuthAccessToken[]>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/tokens`,
  );
}
