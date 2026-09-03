import "server-only";

import { NextResponse } from "next/server";
import { randomUUID } from "node:crypto";

import { auth } from "@/auth";
import { UpstreamError, apiFetchWithTiming, type RequestOptions } from "./api-server";

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
  const startedAt = performance.now();
  const fallbackRequestId = options.requestId ?? randomUUID();
  try {
    let authToken: string | null | undefined = options.authToken;
    if (authToken === undefined) {
      const session = await auth();
      authToken = session?.googleIdToken ?? null;
    }
    if (!authToken) {
      return NextResponse.json(
        { error: { code: "unauthorized", message: "Not signed in." } },
        { status: 401, headers: timingHeaders(new Headers(), fallbackRequestId, startedAt) },
      );
    }
    const requestId = fallbackRequestId;
    const { payload, responseHeaders } = await apiFetchWithTiming<T>(path, {
      ...options,
      authToken,
      requestId,
    });
    const headers = timingHeaders(responseHeaders, payload.meta.request_id || requestId, startedAt);
    return NextResponse.json(payload, { headers });
  } catch (err: unknown) {
    if (err instanceof Error && err.name === "AbortError") {
      throw err;
    }
    if (err instanceof UpstreamError) {
      const headers = timingHeaders(
        err.responseHeaders,
        fallbackRequestId,
        startedAt,
      );
      return NextResponse.json(
        err.body ?? { error: { code: "upstream_error", message: err.message } },
        { status: err.status, headers },
      );
    }
    const headers = timingHeaders(new Headers(), fallbackRequestId, startedAt);
    return NextResponse.json(
      { error: { code: "internal_error", message: "Failed to reach API." } },
      { status: 502, headers },
    );
  }
}

function timingHeaders(
  upstreamHeaders: Headers,
  fallbackRequestId: string,
  startedAt: number,
): Headers {
  const headers = new Headers();
  headers.set("X-Request-Id", upstreamHeaders.get("x-request-id") ?? fallbackRequestId);
  headers.set("X-Bff-Upstream-Duration-Ms", `${(performance.now() - startedAt).toFixed(1)}`);
  for (const header of ["x-api-duration-ms", "x-repository-duration-ms"]) {
    const value = upstreamHeaders.get(header);
    if (value !== null) headers.set(header, value);
  }
  return headers;
}

// Re-exported from the pure query-params module so BFF routes keep importing
// `searchParamsToQuery` from "@/lib/proxy". The pure impl preserves repeated
// keys (multi-value filters such as entity_key/source_key) instead of
// collapsing them to the last value via Object.fromEntries.
export { searchParamsToQuery } from "./query-params";

/**
 * Helper for Server Components / Route Handlers that call apiFetch directly.
 * Returns the signed-in user's id_token or null.
 */
export async function getSessionAuthToken(): Promise<string | null> {
  const session = await auth();
  return session?.googleIdToken ?? null;
}
