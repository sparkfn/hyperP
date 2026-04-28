import type { NextResponse } from "next/server";

import type { ApiKey, ApiKeyCreated, CreateApiKeyRequest } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

/** List all active API keys. */
export async function GET(): Promise<NextResponse> {
  return proxyToApi<ApiKey[]>("/admin/api-keys");
}

/** Create a new API key. The plain secret is returned once only. */
export async function POST(request: Request): Promise<NextResponse> {
  const body: CreateApiKeyRequest = await request.json();
  // Pass as plain object — apiFetch handles JSON serialisation once.
  return proxyToApi<ApiKeyCreated>("/admin/api-keys", {
    method: "POST",
    body,
  });
}