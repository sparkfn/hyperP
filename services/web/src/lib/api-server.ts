// Server-only API client. Do NOT import from client components.
// Used by Next.js Route Handlers and Server Components to talk to FastAPI.

import "server-only";

import type { ApiError, ApiResponse } from "./api-types";

const API_BASE_URL: string = process.env.API_BASE_URL ?? "http://localhost:3000/v1";

export class UpstreamError extends Error {
  public readonly status: number;
  public readonly body: ApiError | null;

  constructor(status: number, body: ApiError | null, message: string) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  // Next.js fetch cache hint. Default: no cache (fresh data).
  revalidate?: number | false;
}

function buildUrl(path: string, query: RequestOptions["query"]): string {
  const url = new URL(`${API_BASE_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === null || value === undefined) continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
  const url: string = buildUrl(path, options.query);
  const init: RequestInit & { next?: { revalidate: number | false } } = {
    method: options.method ?? "GET",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
    },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    next: { revalidate: options.revalidate ?? 0 },
  };

  const response: Response = await fetch(url, init);
  const text: string = await response.text();
  const parsed: unknown = text.length > 0 ? JSON.parse(text) : null;

  if (!response.ok) {
    const errBody: ApiError | null =
      parsed !== null && typeof parsed === "object" && "error" in (parsed as Record<string, unknown>)
        ? (parsed as ApiError)
        : null;
    throw new UpstreamError(response.status, errBody, errBody?.error.message ?? response.statusText);
  }

  return parsed as ApiResponse<T>;
}
