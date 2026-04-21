import type { NextResponse } from "next/server";

import type { UnmergeResponseBody } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<UnmergeResponseBody>("/persons/unmerge", { method: "POST", body });
}
