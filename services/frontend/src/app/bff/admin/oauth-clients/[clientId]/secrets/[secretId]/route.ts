import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(
  _request: Request,
  context: { params: Promise<{ clientId: string; secretId: string }> },
): Promise<NextResponse> {
  const { clientId, secretId } = await context.params;
  return proxyToApi<void>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/secrets/${encodeURIComponent(secretId)}/revoke`,
    { method: "POST" },
  );
}
