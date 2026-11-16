import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi, searchParamsToQuery } = vi.hoisted(() => ({
  proxyToApi: vi.fn(),
  searchParamsToQuery: vi.fn((params: URLSearchParams) => {
    const query: Record<string, string> = {};
    for (const [key, value] of params.entries()) {
      query[key] = value;
    }
    return query;
  }),
}));

vi.mock("@/lib/proxy", () => ({ proxyToApi, searchParamsToQuery }));

import { dynamic, GET as listProfiles } from "../profiles/route";
import { GET as getProfile } from "../profiles/[profileId]/route";

beforeEach(() => {
  proxyToApi.mockReset();
  searchParamsToQuery.mockClear();
});

describe("profiles BFF route", () => {
  it("forwards search, status, and cursor pagination to the upstream proxy", async () => {
    const response = new Response("profiles", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request(
      "http://localhost/bff/profiles?q=ada&status=open&cursor=MjA%3D&limit=10",
    );

    expect(await listProfiles(request)).toBe(response);
    expect(dynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/profiles", {
      query: { q: "ada", status: "open", cursor: "MjA=", limit: "10" },
      signal: request.signal,
    });
  });
});

describe("profile detail BFF route", () => {
  it("encodes the profile id and forwards browser cancellation", async () => {
    const response = new Response("profile", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request("http://localhost/bff/profiles/profile%2F1");

    expect(
      await getProfile(request, { params: Promise.resolve({ profileId: "profile/1" }) }),
    ).toBe(response);
    expect(proxyToApi).toHaveBeenCalledWith("/profiles/profile%2F1", {
      signal: request.signal,
    });
  });
});
