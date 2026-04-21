import "server-only";

import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { UpstreamError, apiFetch, type RequestOptions } from "./api-server";
import type { ApiResponse } from "./api-types";

/**
 * Forward a browser request to the FastAPI backend and translate
 * `UpstreamError`s into `NextResponse`s. Use from Route Handlers only.
 *
 * Automatically attaches the signed-in user's Google ID token as a Bearer
 * header — so the BFF is the single place where the upstream call is
 * authenticated. Callers that already supply `authToken` (e.g. server
 * components calling apiFetch directly) keep the explicit value.
 */
export async function proxyToApi<T>(path: string, options: RequestOptions = {}): Promise<NextResponse> {
  try {
    let authToken: string | null | undefined = options.authToken;
    if (authToken === undefined) {
      const session = await auth();
      authToken = session?.googleIdToken ?? null;
    }
    if (!authToken) {
      return NextResponse.json(
        { error: { code: "unauthorized", message: "Not signed in." } },
        { status: 401 },
      );
    }
    const result: ApiResponse<T> = await apiFetch<T>(path, { ...options, authToken });
    return NextResponse.json(result);
  } catch (err: unknown) {
    if (err instanceof UpstreamError) {
      return NextResponse.json(
        err.body ?? { error: { code: "upstream_error", message: err.message } },
        { status: err.status },
      );
    }
    return NextResponse.json(
      { error: { code: "internal_error", message: "Failed to reach API." } },
      { status: 502 },
    );
  }
}

/** Convert a `URLSearchParams` instance to a plain string-valued query object. */
export function searchParamsToQuery(searchParams: URLSearchParams): Record<string, string> {
  return Object.fromEntries(searchParams.entries());
}

/**
 * Helper for Server Components / Route Handlers that call apiFetch directly.
 * Returns the signed-in user's id_token or null.
 */
export async function getSessionAuthToken(): Promise<string | null> {
  const session = await auth();
  return session?.googleIdToken ?? null;
}
