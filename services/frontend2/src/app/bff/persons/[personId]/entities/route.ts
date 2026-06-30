import type { NextResponse } from "next/server";

import type { PersonEntitySummary } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  return proxyToApi<PersonEntitySummary[]>(
    `/persons/${encodeURIComponent(personId)}/entities`,
  );
}
