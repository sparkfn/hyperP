import type { NextResponse } from "next/server";

import type { DeleteReportResponse, ReportDetail } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ reportKey: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { reportKey } = await context.params;
  return proxyToApi<ReportDetail>(`/reports/${encodeURIComponent(reportKey)}`);
}

export async function PATCH(request: Request, context: RouteContext): Promise<NextResponse> {
  const { reportKey } = await context.params;
  const body: unknown = await request.json();
  return proxyToApi<ReportDetail>(`/reports/${encodeURIComponent(reportKey)}`, {
    method: "PATCH",
    body,
  });
}

export async function DELETE(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { reportKey } = await context.params;
  return proxyToApi<DeleteReportResponse>(`/reports/${encodeURIComponent(reportKey)}`, {
    method: "DELETE",
  });
}
