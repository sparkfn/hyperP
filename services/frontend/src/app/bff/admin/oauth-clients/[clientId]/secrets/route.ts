import { NextResponse } from "next/server";

import type {
  CreateOAuthClientSecretRequest,
  OAuthClientSecretCreated,
} from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

function isCreateOAuthClientSecretRequest(
  value: unknown,
): value is CreateOAuthClientSecretRequest {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  if (
    !("expires_in_days" in value) ||
    (typeof value.expires_in_days !== "number" && value.expires_in_days !== null)
  ) {
    return false;
  }

  return true;
}

export async function POST(
  request: Request,
  context: { params: Promise<{ clientId: string }> },
): Promise<NextResponse> {
  const { clientId } = await context.params;
  const raw: unknown = await request.json();
  if (!isCreateOAuthClientSecretRequest(raw)) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  const body: CreateOAuthClientSecretRequest = raw;
  return proxyToApi<OAuthClientSecretCreated>(
    `/admin/oauth-clients/${encodeURIComponent(clientId)}/secrets`,
    { method: "POST", body },
  );
}
