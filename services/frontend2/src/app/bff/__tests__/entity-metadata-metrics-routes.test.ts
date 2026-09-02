import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));
vi.mock("@/lib/proxy", () => ({ proxyToApi }));
import { GET as metadata } from "../entities/metadata/route";
import { GET as metrics } from "../entities/metrics/route";

beforeEach(() => proxyToApi.mockReset());
describe("entity split BFF routes", () => {
  it("forwards metadata and exact-metrics reads independently with cancellation", async () => {
    proxyToApi.mockResolvedValue(new Response("ok"));
    const request = new Request("https://example.test/bff/entities/metadata");
    await metadata(request); await metrics(request);
    expect(proxyToApi).toHaveBeenNthCalledWith(1, "/entities/metadata", { signal: request.signal });
    expect(proxyToApi).toHaveBeenNthCalledWith(2, "/entities/metrics", { signal: request.signal });
  });
});
