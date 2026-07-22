import { describe, expect, it } from "vitest";

import type { ApiResponse } from "../api-types";
import { normalizePaginatedPage } from "../paginated-page";

function response(
  data: string[],
  totalCount: number | null | undefined,
  nextCursor: string | null,
): ApiResponse<string[]> {
  return {
    data,
    meta: {
      request_id: "request-1",
      next_cursor: nextCursor,
      total_count: totalCount,
    },
  };
}

describe("normalizePaginatedPage", () => {
  it("clears rows and the cursor when the response total is zero", () => {
    expect(normalizePaginatedPage(response(["stale-row"], 0, "stale-cursor"))).toEqual({
      rows: [],
      total: 0,
      nextCursor: null,
    });
  });

  it("preserves rows and pagination for a non-empty response", () => {
    expect(normalizePaginatedPage(response(["person-1"], 1, "next-page"))).toEqual({
      rows: ["person-1"],
      total: 1,
      nextCursor: "next-page",
    });
  });

  it("preserves response data when the total is unavailable", () => {
    expect(normalizePaginatedPage(response(["person-1"], undefined, null))).toEqual({
      rows: ["person-1"],
      total: null,
      nextCursor: null,
    });
  });
});
