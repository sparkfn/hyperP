import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({ proxyToApi }));

import { dynamic, GET } from "../persons/[personId]/crm/metrics/route";

beforeEach(() => {
  proxyToApi.mockReset();
});

describe("person CRM metrics BFF route", () => {
  it("proxies one person through the authenticated API", async () => {
    const response = new Response("crm metrics", { status: 200 });
    proxyToApi.mockResolvedValue(response);

    const result = await GET(
      new Request("https://example.test/bff/persons/person%2Fone/crm/metrics"),
      { params: Promise.resolve({ personId: "person/one" }) },
    );

    expect(dynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/persons/person%2Fone/crm/metrics");
    expect(result).toBe(response);
  });
});
