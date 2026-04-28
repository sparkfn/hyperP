import type { NextResponse } from "next/server";

import type { PublicLink } from "@/lib/api-types";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function POST(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  return proxyToApi<PublicLink>(`/persons/${encodeURIComponent(personId)}/public-link`, {
    method: "POST",
  });
}
