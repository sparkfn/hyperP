import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({
  proxyToApi,
  searchParamsToQuery: (params: URLSearchParams): URLSearchParams => params,
}));

import { GET } from "../review-cases/route";

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
