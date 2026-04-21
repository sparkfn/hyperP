import type { NextResponse } from "next/server";

import type { ManualMergeResponseBody } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<ManualMergeResponseBody>("/persons/manual-merge", { method: "POST", body });
}
