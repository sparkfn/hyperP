import type { NextResponse } from "next/server";

import type { PersonSourceRecord } from "@/lib/api-types-person";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

interface RouteContext {
  params: Promise<{ personId: string }>;
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { personId } = await context.params;
  return proxyToApi<PersonSourceRecord[]>(
    `/persons/${encodeURIComponent(personId)}/source-records`,
  );
}
