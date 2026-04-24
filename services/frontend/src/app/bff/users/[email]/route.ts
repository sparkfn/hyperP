import type { NextResponse } from "next/server";

import type { UserResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

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
