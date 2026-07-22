import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi, searchParamsToQuery } = vi.hoisted(() => ({
  proxyToApi: vi.fn(),
  searchParamsToQuery: vi.fn((params: URLSearchParams) => ({
    analysis_type: params.getAll("analysis_type"),
    cursor: params.get("cursor"),
    limit: params.get("limit"),
  })),
}));

vi.mock("@/lib/proxy", () => ({ proxyToApi, searchParamsToQuery }));

import { GET as getCurrent, dynamic as currentDynamic } from "../persons/[personId]/profile-analyses/route";
import { GET as getHistory, dynamic as historyDynamic } from "../persons/[personId]/profile-analyses/history/route";

beforeEach(() => {
  proxyToApi.mockReset();
  searchParamsToQuery.mockClear();
});

describe("profile analysis BFF routes", () => {
  it("forwards the current request with an encoded Person ID", async () => {
    const response = new Response("current", { status: 200 });
    proxyToApi.mockResolvedValue(response);

    const result = await getCurrent(
      new Request("https://example.test/bff/persons/person%2Fone/profile-analyses"),
      { params: Promise.resolve({ personId: "person/one" }) },
    );

    expect(currentDynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/persons/person%2Fone/profile-analyses");
    expect(result).toBe(response);
  });

  it("preserves history query parameters while forwarding", async () => {
    const response = new Response("history", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request(
      "https://example.test/bff/persons/person-1/profile-analyses/history"
      + "?analysis_type=sales&analysis_type=contact_tracing&cursor=Mg%3D%3D&limit=20",
    );

    const result = await getHistory(request, {
      params: Promise.resolve({ personId: "person-1" }),
    });

    expect(historyDynamic).toBe("force-dynamic");
    expect(searchParamsToQuery).toHaveBeenCalledOnce();
    expect(proxyToApi).toHaveBeenCalledWith(
      "/persons/person-1/profile-analyses/history",
      {
        query: {
          analysis_type: ["sales", "contact_tracing"],
          cursor: "Mg==",
          limit: "20",
        },
      },
    );
    expect(result).toBe(response);
  });

  it("returns the proxy error response unchanged", async () => {
    const response = new Response("upstream error", { status: 503 });
    proxyToApi.mockResolvedValue(response);

    const result = await getCurrent(
      new Request("https://example.test/bff/persons/person-1/profile-analyses"),
      { params: Promise.resolve({ personId: "person-1" }) },
    );

    expect(result.status).toBe(503);
    expect(await result.text()).toBe("upstream error");
  });
});
