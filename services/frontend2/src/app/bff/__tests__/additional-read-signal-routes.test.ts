import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi, searchParamsToQuery } = vi.hoisted(() => ({
  proxyToApi: vi.fn(),
  searchParamsToQuery: vi.fn(() => ({ q: "person" })),
}));
vi.mock("@/lib/proxy", () => ({ proxyToApi, searchParamsToQuery }));

import { GET as getGraphNode } from "../persons/graph/node/route";
import { GET as getSearch } from "../persons/search/route";
import { GET as getReviewByDecision } from "../review-cases/by-match-decision/[matchDecisionId]/route";

beforeEach(() => {
  proxyToApi.mockReset();
  searchParamsToQuery.mockClear();
  proxyToApi.mockResolvedValue(new Response("ok"));
});

describe("additional expensive read cancellation", () => {
  it("forwards incoming signals through search, graph-node, and decision-detail routes", async () => {
    const request = new Request("https://example.test/bff/read?q=person");

    await getSearch(request);
    await getGraphNode(request);
    await getReviewByDecision(request, {
      params: Promise.resolve({ matchDecisionId: "decision/1" }),
    });

    expect(proxyToApi).toHaveBeenNthCalledWith(1, "/persons/search", {
      query: { q: "person" },
      signal: request.signal,
    });
    expect(proxyToApi).toHaveBeenNthCalledWith(2, "/persons/graph/node", {
      query: { q: "person" },
      signal: request.signal,
    });
    expect(proxyToApi).toHaveBeenNthCalledWith(
      3,
      "/review-cases/by-match-decision/decision%2F1",
      { signal: request.signal },
    );
  });
});
