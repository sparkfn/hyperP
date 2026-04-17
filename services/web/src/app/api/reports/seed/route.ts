import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function POST(): Promise<NextResponse> {
  return proxyToApi<string[]>("/reports/seed", { method: "POST" });
}
