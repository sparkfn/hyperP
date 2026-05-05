import type { NextResponse } from "next/server";

import type { UserBulkCreateResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<UserBulkCreateResponse>("/users/bulk", {
    method: "POST",
    body,
  });
}
