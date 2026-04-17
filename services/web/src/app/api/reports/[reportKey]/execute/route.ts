import type { NextResponse } from "next/server";

import type { ReportResult } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ reportKey: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  const { reportKey } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<ReportResult>(`/reports/${encodeURIComponent(reportKey)}/execute`, {
    method: "POST",
    body,
  });
}
