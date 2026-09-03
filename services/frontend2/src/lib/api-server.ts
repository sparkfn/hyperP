// Server-only API client. Do NOT import from client components.
// Used by Next.js Route Handlers and Server Components to talk to FastAPI.

import "server-only";

import { auth } from "@/auth";
import { buildApiUrl } from "./api-url";
import type { ApiError, ApiResponse, ResponseMeta } from "./api-types";
import { appendQueryParams, type QueryParams } from "./query-params";

export class UpstreamError extends Error {
  public readonly status: number;
  public readonly body: ApiError | null;
  public readonly responseHeaders: Headers;

  constructor(status: number, body: ApiError | null, message: string, responseHeaders: Headers) {
    super(message);
    this.status = status;
    this.body = body;
    this.responseHeaders = responseHeaders;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: QueryParams;
  body?: unknown;
  // Next.js fetch cache hint. Default: no cache (fresh data).
  revalidate?: number | false;
  // Bearer token forwarded as `Authorization: Bearer <token>` when provided.
  authToken?: string | null;
  // Stable retry key for write endpoints that require idempotent dispatch.
  idempotencyKey?: string;
  // Abort signal forwarded from a Route Handler's incoming browser request.
  signal?: AbortSignal;
  // Correlation ID generated at the BFF boundary; never a user identifier.
  requestId?: string;
}

export interface ApiFetchResult<T> {
  payload: ApiResponse<T>;
  responseHeaders: Headers;
}

function buildUrl(path: string, query: RequestOptions["query"]): string {
  const url: URL = buildApiUrl(path);
  appendQueryParams(url, query);
  return url.toString();
}

export async function apiFetchWithTiming<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiFetchResult<T>> {
  const url: string = buildUrl(path, options.query);
  const headers: Record<string, string> = {
    "content-type": "application/json",
    accept: "application/json",
  };
  // authToken semantics:
  //   undefined → fall back to the signed-in user's Google id_token.
  //   string    → use as-is.
  //   null      → explicitly send no Authorization header.
  let token: string | null | undefined = options.authToken;
  if (token === undefined) {
    const session = await auth();
    token = session?.googleIdToken ?? null;
  }
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  if (options.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }
  if (options.requestId) {
    headers["X-Request-Id"] = options.requestId;
  }
  const init: RequestInit & { next?: { revalidate: number | false } } = {
    method: options.method ?? "GET",
    headers,
    body: options.body !== undefined
      ? typeof options.body === "string" ? options.body : JSON.stringify(options.body)
      : undefined,
    next: { revalidate: options.revalidate ?? 0 },
    signal: options.signal,
  };

  const response: Response = await fetch(url, init);
  const text: string = await response.text();
  const parsed: unknown = text.length > 0 ? JSON.parse(text) : null;

  if (!response.ok) {
    const errBody: ApiError | null =
      parsed !== null && typeof parsed === "object" && "error" in (parsed as Record<string, unknown>)
        ? (parsed as ApiError)
        : null;
    throw new UpstreamError(
      response.status,
      errBody,
      errBody?.error.message ?? response.statusText,
      response.headers,
    );
  }

  // 204 No Content — return a null payload without requiring a body.
  if (parsed === null) {
    return {
      payload: { data: null as T, meta: { request_id: "", next_cursor: null } as ResponseMeta },
      responseHeaders: response.headers,
    };
  }

  // Auto-wrap bare arrays (e.g. admin endpoints that return list[T] without envelope()).
  if (Array.isArray(parsed)) {
    return {
      payload: { data: parsed as T, meta: { request_id: "", next_cursor: null } as ResponseMeta },
      responseHeaders: response.headers,
    };
  }
  if (typeof parsed !== "object") {
    throw new UpstreamError(
      response.status,
      null,
      "Unexpected response shape from API.",
      response.headers,
    );
  }
  // Auto-wrap bare object responses for endpoints that skip the envelope() wrapper.
  if (!("data" in parsed)) {
    return {
      payload: { data: parsed as T, meta: { request_id: "", next_cursor: null } as ResponseMeta },
      responseHeaders: response.headers,
    };
  }
  return { payload: parsed as ApiResponse<T>, responseHeaders: response.headers };
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  return (await apiFetchWithTiming<T>(path, options)).payload;
}
