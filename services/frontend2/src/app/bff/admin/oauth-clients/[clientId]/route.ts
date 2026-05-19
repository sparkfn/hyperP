import type { NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ clientId: string }>;
}

export async function DELETE(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<null>(`/v1/admin/oauth-clients/${encodeURIComponent(clientId)}`, { method: "DELETE" });
}
