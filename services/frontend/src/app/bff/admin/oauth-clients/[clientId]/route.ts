import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<void>(`/admin/oauth-clients/${encodeURIComponent(clientId)}`, {
    method: "DELETE",
  });
}

export async function POST(
  _request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<void>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/disable`,
    { method: "POST" },
  );
}
