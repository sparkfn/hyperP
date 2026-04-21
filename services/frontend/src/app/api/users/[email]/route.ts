import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface UserResponse {
  email: string;
  google_sub: string;
  role: string;
  entity_key: string | null;
  display_name: string | null;
}

interface RouteContext {
  params: Promise<{ email: string }>;
}

export async function PATCH(request: Request, context: RouteContext): Promise<NextResponse> {
  const { email } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<UserResponse>(`/users/${encodeURIComponent(email)}`, {
    method: "PATCH",
    body,
  });
}
