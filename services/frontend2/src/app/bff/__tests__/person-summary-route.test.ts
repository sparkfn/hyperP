import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({ proxyToApi }));

import { GET, dynamic } from "../persons/summary/route";

beforeEach(() => {
  proxyToApi.mockReset();
});

describe("person summary BFF route", () => {
  it("proxies the aggregate summary through the authenticated API", async () => {
    const response = new Response("summary", { status: 200 });
    proxyToApi.mockResolvedValue(response);

    const result = await GET();

    expect(dynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/persons/summary");
    expect(result).toBe(response);
  });
});
