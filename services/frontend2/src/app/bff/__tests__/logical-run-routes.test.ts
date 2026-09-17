import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyToApi } = vi.hoisted(() => ({ proxyToApi: vi.fn() }));

vi.mock("@/lib/proxy", () => ({ proxyToApi }));

import {
  GET as getLogicalRun,
  dynamic as logicalRunDynamic,
} from "../ingest/logical-runs/[runId]/route";
import {
  POST as pauseLogicalRun,
  dynamic as pauseDynamic,
} from "../ingest/logical-runs/[runId]/pause/route";
import {
  POST as resumeLogicalRun,
  dynamic as resumeDynamic,
} from "../ingest/logical-runs/[runId]/resume/route";

beforeEach(() => proxyToApi.mockReset());

describe("bounded logical-run BFF routes", () => {
  it("forwards an encoded run identifier through the authenticated proxy", async () => {
    const response = new Response("status", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request("https://example.test/bff/ingest/logical-runs/run%2Fone");

    const result = await getLogicalRun(request, {
      params: Promise.resolve({ runId: "run/one" }),
    });

    expect(logicalRunDynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/ingest/logical-runs/run%2Fone", {
      signal: request.signal,
    });
    expect(result).toBe(response);
  });

  it("forwards the pause body without browser-side FastAPI access", async () => {
    const response = new Response("paused", { status: 200 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request("https://example.test/bff/ingest/logical-runs/run-1/pause", {
      method: "POST",
      body: JSON.stringify({
        source_key: "fundbox",
        control_instance_id: "control-1",
        reset_generation: 2,
        reason: "operator investigation",
      }),
    });

    const result = await pauseLogicalRun(request, {
      params: Promise.resolve({ runId: "run-1" }),
    });

    expect(pauseDynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/ingest/logical-runs/run-1/pause", {
      method: "POST",
      body: {
        source_key: "fundbox",
        control_instance_id: "control-1",
        reset_generation: 2,
        reason: "operator investigation",
      },
      signal: request.signal,
    });
    expect(result).toBe(response);
  });

  it("forwards the exact resume identity and preserves upstream errors", async () => {
    const response = new Response("not eligible", { status: 409 });
    proxyToApi.mockResolvedValue(response);
    const request = new Request("https://example.test/bff/ingest/logical-runs/run-1/resume", {
      method: "POST",
      body: JSON.stringify({
        source_key: "fundbox",
        control_instance_id: "control-1",
        reset_generation: 2,
      }),
    });

    const result = await resumeLogicalRun(request, {
      params: Promise.resolve({ runId: "run-1" }),
    });

    expect(resumeDynamic).toBe("force-dynamic");
    expect(proxyToApi).toHaveBeenCalledWith("/ingest/logical-runs/run-1/resume", {
      method: "POST",
      body: {
        source_key: "fundbox",
        control_instance_id: "control-1",
        reset_generation: 2,
      },
      signal: request.signal,
    });
    expect(result).toBe(response);
  });
});
