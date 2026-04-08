import "server-only";

import { NextResponse } from "next/server";

import { UpstreamError, apiFetch, type RequestOptions } from "./api-server";
import type { ApiResponse } from "./api-types";

/**
 * Forward a browser request to the FastAPI backend and translate
 * `UpstreamError`s into `NextResponse`s. Use from Route Handlers only.
 */
export async function proxyToApi<T>(path: string, options: RequestOptions = {}): Promise<NextResponse> {
  try {
    const result: ApiResponse<T> = await apiFetch<T>(path, options);
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
