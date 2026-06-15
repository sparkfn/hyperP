import type { NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ clientId: string; jti: string }>;
}

export async function POST(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId, jti } = await context.params;
  return proxyToApi<null>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/tokens/${encodeURIComponent(jti)}/revoke`,
    { method: "POST" },
  );
}
