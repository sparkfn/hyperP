// Browser-safe API client. Talks to the Next.js BFF (`/bff/*`), never to
// FastAPI directly. Server-side code should use `lib/api-server` instead.

import type { ApiError, ApiResponse } from "./api-types";
import { BASE_PATH, toBasePath } from "./route-paths";

// Next.js does NOT apply the framework basePath to raw fetch() calls (only to
// <Link>/router/next-auth), so origin-relative BFF paths must be prefixed here.
function withBasePath(path: string): string {
  if (path.startsWith("/") && !path.startsWith(`${BASE_PATH}/`)) {
    return `${BASE_PATH}${path}`;
  }
  return path;
}

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
  const response: Response = await fetch(withBasePath(path), init);
  const json: unknown = await response.json();

  if (!response.ok) {
    if (response.status === 401) {
      window.location.href = toBasePath("/login");
      throw new BffError(401, "unauthorized", "Session expired.");
    }
    if (isApiError(json)) {
      throw new BffError(response.status, json.error.code, json.error.message);
    }
    throw new BffError(response.status, "unknown_error", response.statusText);
  }
  return json as ApiResponse<T>;
}
