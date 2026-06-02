import type { NextResponse } from "next/server";

import type { PossibleMatchDetail } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string; candidateId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId, candidateId } = await context.params;
  return proxyToApi<PossibleMatchDetail>(
    `/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidateId)}/detail`,
  );
}
