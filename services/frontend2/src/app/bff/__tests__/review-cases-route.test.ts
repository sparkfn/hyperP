import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({
  proxyToApi,
  searchParamsToQuery: (params: URLSearchParams): URLSearchParams => params,
}));

import { GET } from "../review-cases/route";
import { POST as postBulkAction } from "../review-cases/bulk-actions/route";

beforeEach(() => proxyToApi.mockReset());

describe("review-cases BFF route", () => {
  it("forwards browser cancellation to the authenticated upstream proxy", async () => {
    const response = new Response("ok");
    proxyToApi.mockResolvedValue(response);
    const request = new Request("https://example.test/bff/review-cases?resolved=false");

    expect(await GET(request)).toBe(response);
    expect(proxyToApi).toHaveBeenCalledWith("/review-cases", {
      query: new URL(request.url).searchParams,
      signal: request.signal,
    });
  });
});

describe("review-cases bulk-actions BFF route", () => {
  it("forwards the bulk action payload to the upstream proxy", async () => {
    const response = new Response("ok");
    proxyToApi.mockResolvedValue(response);
    const body = {
      review_case_ids: ["case-1", "case-2"],
      action_type: "resolve",
      reason_code: "confirmed_match",
      notes: null,
      follow_up_at: null,
    };
    const request = new Request("https://example.test/bff/review-cases/bulk-actions", {
      method: "POST",
      body: JSON.stringify(body),
    });

    expect(await postBulkAction(request)).toBe(response);
    expect(proxyToApi).toHaveBeenCalledWith("/review-cases/bulk-actions", {
      method: "POST",
      body,
    });
  });
});
