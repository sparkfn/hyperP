import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi, searchParamsToQuery } = vi.hoisted(() => ({
  proxyToApi: vi.fn(),
  searchParamsToQuery: vi.fn((params: URLSearchParams) => {
    const query: Record<string, string | string[]> = {};
    for (const [key, value] of params.entries()) {
      const existing = query[key];
      if (existing === undefined) query[key] = value;
      else if (Array.isArray(existing)) existing.push(value);
      else query[key] = [existing, value];
    }
    return query;
  }),
}));

vi.mock("@/lib/proxy", () => ({ proxyToApi, searchParamsToQuery }));

import { dynamic, GET } from "../persons/route";

beforeEach(() => {
  proxyToApi.mockReset();
  searchParamsToQuery.mockClear();
});

describe("persons BFF route", () => {
  it("preserves repeated filters and forces exact counting off", async () => {
    const response = new Response("persons", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request(
      "http://localhost/bff/persons?entity_key=one&entity_key=two&include_total=true&limit=25",
    );

    expect(await GET(request)).toBe(response);
    expect(dynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/persons", {
      query: {
        entity_key: ["one", "two"],
        include_total: "false",
        limit: "25",
      },
      signal: request.signal,
    });
  });
});
