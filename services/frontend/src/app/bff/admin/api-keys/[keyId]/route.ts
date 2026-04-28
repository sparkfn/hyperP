import type { NextResponse } from "next/server";

import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ keyId: string }> },
): Promise<NextResponse> {
  const { keyId } = await context.params;
  return proxyToApi<void>(`/admin/api-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  });
}

/** Revoke an API key (soft-revoke). */
export async function POST(
  _request: Request,
  context: { params: Promise<{ keyId: string }> },
): Promise<NextResponse> {
  const { keyId } = await context.params;
  return proxyToApi<void>(`/admin/api-keys/${encodeURIComponent(keyId)}/revoke`, {
    method: "POST",
  });
}