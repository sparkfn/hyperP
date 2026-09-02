import type { NextResponse } from "next/server";

import type { EntityMetadata } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<NextResponse> {
  return proxyToApi<EntityMetadata[]>("/entities/metadata", { signal: request.signal });
}
