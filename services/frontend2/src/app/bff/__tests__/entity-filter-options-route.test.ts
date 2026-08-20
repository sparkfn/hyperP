import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({ proxyToApi }));

import { dynamic, GET } from "../entities/filter-options/route";

beforeEach(() => {
  proxyToApi.mockReset();
});

describe("entity filter options BFF route", () => {
  it("proxies only the lightweight entity-filter contract", async () => {
    const response = new Response("options", { status: 200 });
    proxyToApi.mockResolvedValue(response);

    expect(await GET()).toBe(response);
    expect(dynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/entities/filter-options");
  });
});
