import { afterEach, describe, expect, it, vi } from "vitest";

const { auth } = vi.hoisted(() => ({ auth: vi.fn() }));

vi.mock("server-only", () => ({}));
vi.mock("@/auth", () => ({ auth }));

import { apiFetchWithTiming } from "../api-server";

afterEach(() => {
  vi.unstubAllGlobals();
  auth.mockReset();
});

describe("apiFetchWithTiming", () => {
  it("forwards the abort signal and request ID to the upstream fetch", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => new Response(
        JSON.stringify({ data: { person_id: "person-1" }, meta: { request_id: "api-1", next_cursor: null } }),
        {
          headers: {
            "content-type": "application/json",
            "x-api-duration-ms": "12.3",
            "x-repository-duration-ms": "8.4",
          },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await apiFetchWithTiming<{ person_id: string }>("/persons/person-1", {
      authToken: "token",
      requestId: "bff-request-1",
      signal: controller.signal,
    });

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.signal).toBe(controller.signal);
    expect(new Headers(init?.headers).get("x-request-id")).toBe("bff-request-1");
    expect(result.responseHeaders.get("x-api-duration-ms")).toBe("12.3");
    expect(result.responseHeaders.get("x-repository-duration-ms")).toBe("8.4");
  });
});
