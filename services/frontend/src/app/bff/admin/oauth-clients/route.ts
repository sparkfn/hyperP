import { NextResponse } from "next/server";

import type {
  CreateOAuthClientRequest,
  OAuthClient,
  OAuthClientCreated,
} from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

function isCreateOAuthClientRequest(
  value: unknown,
): value is CreateOAuthClientRequest {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  if (!("name" in value) || typeof value.name !== "string") {
    return false;
  }

  if (
    !("entity_key" in value) ||
    (typeof value.entity_key !== "string" && value.entity_key !== null)
  ) {
    return false;
  }

  if (
    !("scopes" in value) ||
    !Array.isArray(value.scopes) ||
    !value.scopes.every((scope: unknown): scope is string => typeof scope === "string")
  ) {
    return false;
  }

  if (
    !("secret_expires_in_days" in value) ||
    (typeof value.secret_expires_in_days !== "number" &&
      value.secret_expires_in_days !== null)
  ) {
    return false;
  }

  return true;
}

export async function GET(): Promise<NextResponse> {
  return proxyToApi<OAuthClient[]>("/admin/oauth-clients");
}

export async function POST(request: Request): Promise<NextResponse> {
  const raw: unknown = await request.json();
  if (!isCreateOAuthClientRequest(raw)) {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  const body: CreateOAuthClientRequest = raw;
  return proxyToApi<OAuthClientCreated>("/admin/oauth-clients", {
    method: "POST",
    body,
  });
}
