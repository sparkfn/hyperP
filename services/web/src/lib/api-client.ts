// Browser-safe API client. Talks to the Next.js BFF (`/api/*`), never to
// FastAPI directly. Server-side code should use `lib/api-server` instead.

import type { ApiError, ApiResponse } from "./api-types";

export class BffError extends Error {
  public readonly status: number;
  public readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as { error: unknown }).error === "object"
  );
}

export async function bffFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return (await bffFetchEnvelope<T>(path, init)).data;
}

export async function bffFetchEnvelope<T>(path: string, init?: RequestInit): Promise<ApiResponse<T>> {
  const response: Response = await fetch(path, init);
  const json: unknown = await response.json();

  if (!response.ok) {
    if (isApiError(json)) {
      throw new BffError(response.status, json.error.code, json.error.message);
    }
    throw new BffError(response.status, "unknown_error", response.statusText);
  }
  return json as ApiResponse<T>;
}
