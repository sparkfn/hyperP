import type { NextResponse } from "next/server";

import type { EntityFilterOption } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  return proxyToApi<EntityFilterOption[]>("/entities/filter-options");
}
