import { afterEach, describe, expect, it, vi } from "vitest";

const { auth, apiFetchWithTiming } = vi.hoisted(() => ({
  auth: vi.fn(),
  apiFetchWithTiming: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("@/auth", () => ({ auth }));
vi.mock("../api-server", () => ({
  UpstreamError: class UpstreamError extends Error {},
  apiFetchWithTiming,
}));

import { proxyToApi } from "../proxy";

afterEach(() => {
  auth.mockReset();
  apiFetchWithTiming.mockReset();
});

describe("proxyToApi timing headers", () => {
  it("passes cancellation through and returns safe upstream timing headers", async () => {
    const controller = new AbortController();
    apiFetchWithTiming.mockResolvedValue({
      payload: {
        data: { person_id: "person-1" },
        meta: { request_id: "api-request-1", next_cursor: null },
      },
      responseHeaders: new Headers({
        "x-request-id": "api-request-1",
        "x-api-duration-ms": "12.3",
        "x-repository-duration-ms": "8.4",
      }),
    });

    const response = await proxyToApi<{ person_id: string }>("/persons/person-1", {
      authToken: "token",
      requestId: "bff-request-1",
      signal: controller.signal,
    });

    expect(apiFetchWithTiming).toHaveBeenCalledWith("/persons/person-1", {
      authToken: "token",
      requestId: "bff-request-1",
      signal: controller.signal,
    });
    expect(response.headers.get("x-request-id")).toBe("api-request-1");
    expect(response.headers.get("x-api-duration-ms")).toBe("12.3");
    expect(response.headers.get("x-repository-duration-ms")).toBe("8.4");
    expect(response.headers.get("x-bff-upstream-duration-ms")).not.toBeNull();
  });
});
