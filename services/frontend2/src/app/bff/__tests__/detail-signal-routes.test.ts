import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));
vi.mock("@/lib/proxy", () => ({ proxyToApi }));

import { GET as getCandidate } from "../persons/[personId]/shared-identifiers/[candidateId]/detail/route";
import { GET as getReview } from "../review-cases/[reviewCaseId]/route";

beforeEach(() => proxyToApi.mockReset());

describe("detail BFF cancellation", () => {
  it("forwards incoming signals for candidate and review detail reads", async () => {
    proxyToApi.mockResolvedValue(new Response("ok"));
    const request = new Request("https://example.test/bff/detail");
    await getCandidate(request, { params: Promise.resolve({ personId: "p 1", candidateId: "p2" }) });
    await getReview(request, { params: Promise.resolve({ reviewCaseId: "r1" }) });
    expect(proxyToApi).toHaveBeenNthCalledWith(1, "/persons/p%201/shared-identifiers/p2/detail", { signal: request.signal });
    expect(proxyToApi).toHaveBeenNthCalledWith(2, "/review-cases/r1", { signal: request.signal });
  });
});
