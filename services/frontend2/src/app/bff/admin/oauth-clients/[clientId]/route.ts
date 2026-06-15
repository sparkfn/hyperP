import type { NextResponse } from "next/server";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ clientId: string }>;
}

export async function PATCH(request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<null>(`/admin/oauth-clients/${encodeURIComponent(clientId)}`, { method: "PATCH", body });
}

export async function DELETE(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<null>(`/admin/oauth-clients/${encodeURIComponent(clientId)}`, { method: "DELETE" });
}
