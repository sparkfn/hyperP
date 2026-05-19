import type { NextResponse } from "next/server";
import type { OAuthClient, OAuthClientCreated } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  return proxyToApi<OAuthClient[]>("/v1/admin/oauth-clients");
}

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<OAuthClientCreated>("/v1/admin/oauth-clients", { method: "POST", body });
}
