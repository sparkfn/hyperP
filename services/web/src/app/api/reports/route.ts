import type { NextResponse } from "next/server";

import type { ReportDetail, ReportSummary } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  return proxyToApi<ReportSummary[]>("/reports");
}

export async function POST(request: Request): Promise<NextResponse> {
  const body: unknown = await request.json();
  return proxyToApi<ReportDetail>("/reports", { method: "POST", body });
}
