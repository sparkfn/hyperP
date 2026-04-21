import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface MeResponse {
  email: string;
  google_sub: string;
  role: string;
  entity_key: string | null;
  display_name: string | null;
}

export async function GET(): Promise<NextResponse> {
  return proxyToApi<MeResponse>("/auth/me");
}
