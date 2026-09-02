import { afterEach, describe, expect, it, vi } from "vitest";

const { auth, apiFetchWithTiming } = vi.hoisted(() => ({
  auth: vi.fn(),
  apiFetchWithTiming: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("@/auth", () => ({ auth }));
vi.mock("../api-server", () => ({
  UpstreamError: class UpstreamError extends Error {
    public readonly status: number;
    public readonly body: {
      error: { code: string; message: string };
      meta: { request_id: string; next_cursor: null };
    } | null;
    public readonly responseHeaders: Headers;

    constructor(
      status: number,
      body: {
        error: { code: string; message: string };
        meta: { request_id: string; next_cursor: null };
      } | null,
      message: string,
      responseHeaders: Headers,
    ) {
      super(message);
      this.status = status;
      this.body = body;
      this.responseHeaders = responseHeaders;
    }
  },
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

  it("keeps safe upstream timing headers on API errors", async () => {
    const { UpstreamError } = await import("../api-server");
    apiFetchWithTiming.mockRejectedValue(new UpstreamError(
      504,
      {
        error: { code: "timeout", message: "Timed out." },
        meta: { request_id: "api-timeout-1", next_cursor: null },
      },
      "Timed out.",
      new Headers({
        "x-request-id": "api-timeout-1",
        "x-api-duration-ms": "1000.0",
        "x-repository-duration-ms": "995.0",
      }),
    ));

    const response = await proxyToApi<{ person_id: string }>("/persons/person-1", {
      authToken: "token",
      requestId: "bff-timeout-1",
    });

    expect(response.status).toBe(504);
    expect(response.headers.get("x-request-id")).toBe("api-timeout-1");
    expect(response.headers.get("x-api-duration-ms")).toBe("1000.0");
    expect(response.headers.get("x-repository-duration-ms")).toBe("995.0");
    expect(response.headers.get("x-bff-upstream-duration-ms")).not.toBeNull();
  });

  it("does not translate an abort into a generic upstream response", async () => {
    apiFetchWithTiming.mockRejectedValue(new DOMException("cancelled", "AbortError"));

    await expect(proxyToApi<{ person_id: string }>("/persons/person-1", {
      authToken: "token",
    })).rejects.toMatchObject({ name: "AbortError" });
  });

  it("keeps a request ID and BFF timing on local authorization errors", async () => {
    auth.mockResolvedValue(null);

    const response = await proxyToApi<{ person_id: string }>("/persons/person-1", {
      requestId: "bff-unauthorized-1",
    });

    expect(response.status).toBe(401);
    expect(response.headers.get("x-request-id")).toBe("bff-unauthorized-1");
    expect(response.headers.get("x-bff-upstream-duration-ms")).not.toBeNull();
  });
});
