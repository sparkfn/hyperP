import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));
vi.mock("@/lib/proxy", () => ({ proxyToApi }));
import { GET as dealGet } from "../persons/[personId]/crm/deal-metrics/route";
import { GET as activityGet } from "../persons/[personId]/crm/activity-metrics/route";

beforeEach(() => proxyToApi.mockReset());
describe("split person CRM metrics BFF routes", () => {
  it("forwards abort signals independently", async () => {
    proxyToApi.mockResolvedValue(new Response("ok"));
    const request = new Request("https://example.test/bff/persons/p%2F1/crm/deal-metrics");
    await dealGet(request, { params: Promise.resolve({ personId: "p/1" }) });
    await activityGet(request, { params: Promise.resolve({ personId: "p/1" }) });
    expect(proxyToApi).toHaveBeenNthCalledWith(1, "/persons/p%2F1/crm/deal-metrics", { signal: request.signal });
    expect(proxyToApi).toHaveBeenNthCalledWith(2, "/persons/p%2F1/crm/activity-metrics", { signal: request.signal });
  });
});
