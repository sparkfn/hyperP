import type { NextResponse } from "next/server";
import type { RotateSecretResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ clientId: string }>;
}

export async function POST(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { clientId } = await context.params;
  return proxyToApi<RotateSecretResponse>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/rotate-secret`,
    { method: "POST" },
  );
}
