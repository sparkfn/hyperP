import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({ proxyToApi }));

import { GET, dynamic } from "../persons/summary/route";
import { GET as getCore } from "../persons/summary/core/route";
import { GET as getCrm } from "../persons/summary/crm/route";

beforeEach(() => {
  proxyToApi.mockReset();
});

describe("person summary BFF route", () => {
  it("proxies the aggregate summary through the authenticated API", async () => {
    const response = new Response("summary", { status: 200 });
    proxyToApi.mockResolvedValue(response);

    const request = new Request("https://example.test/bff/persons/summary");
    const result = await GET(request);

    expect(dynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/persons/summary", { signal: request.signal });
    expect(result).toBe(response);
  });

  it("loads core and CRM summary contracts independently", async () => {
    const response = new Response("summary", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request("https://example.test/bff/persons/summary/core");

    await getCore(request);
    await getCrm(request);

    expect(proxyToApi).toHaveBeenNthCalledWith(1, "/persons/summary/core", {
      signal: request.signal,
    });
    expect(proxyToApi).toHaveBeenNthCalledWith(2, "/persons/summary/crm", {
      signal: request.signal,
    });
  });
});
