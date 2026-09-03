import type { NextResponse } from "next/server";

import type { EntityMetrics } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<NextResponse> {
  return proxyToApi<EntityMetrics[]>("/entities/metrics", { signal: request.signal });
}
