import { describe, expect, it } from "vitest";

import {
  claimAbortableDetailRequest,
  claimDetailRequest,
  isAbortError,
  needsDetail,
  ownsDetailRequest,
  releaseAbortableDetailRequest,
  releaseDetailGeneration,
  releaseDetailRequest,
  shouldShowDetailLoading,
} from "../match-detail-in-flight";

describe("match detail in-flight ownership", () => {
  it("does not let an old request completion clear a newer owner", () => {
    const owners = new Map<string, symbol>();
    const oldToken = claimDetailRequest(owners, "case-1");
    expect(oldToken).not.toBeNull();
    if (oldToken === null) throw new Error("expected first request token");

    owners.delete("case-1");
    const newToken = claimDetailRequest(owners, "case-1");
    expect(newToken).not.toBeNull();
    if (newToken === null) throw new Error("expected replacement request token");

    releaseDetailRequest(owners, "case-1", oldToken);
    expect(owners.get("case-1")).toBe(newToken);
  });

  it("cleanup releases only requests claimed by its generation", () => {
    const owners = new Map<string, symbol>();
    const first = claimDetailRequest(owners, "case-1");
    const second = claimDetailRequest(owners, "case-2");
    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    if (first === null || second === null) throw new Error("expected request tokens");

    const generation = new Map([["case-1", first]]);
    releaseDetailGeneration(owners, generation);

    expect(owners.has("case-1")).toBe(false);
    expect(owners.get("case-2")).toBe(second);
  });

  it("recognizes aborted fetches as cancellation rather than an error", () => {
    const abort = new Error("cancelled");
    abort.name = "AbortError";
    expect(isAbortError(abort)).toBe(true);
    expect(isAbortError(new Error("network failed"))).toBe(false);
  });

  it("does not reload a successfully cached detail", () => {
    const owners = new Map<string, symbol>();

    expect(needsDetail({ "case-1": { loaded: true } }, owners, "case-1")).toBe(false);
    expect(needsDetail({}, owners, "case-2")).toBe(true);
  });

  it("shows initial loading and lets an owned retry replace a prior failure", () => {
    expect(shouldShowDetailLoading(undefined, undefined, undefined)).toBe(true);
    expect(shouldShowDetailLoading(undefined, "temporary failure", false)).toBe(false);
    expect(shouldShowDetailLoading({ loaded: true }, "temporary failure", true)).toBe(false);
  });

  it("keeps a replacement request owned when an earlier completion is stale", () => {
    const owners = new Map<string, symbol>();
    const failedRequest = claimDetailRequest(owners, "case-1");
    expect(failedRequest).not.toBeNull();
    if (failedRequest === null) throw new Error("expected initial request token");
    releaseDetailRequest(owners, "case-1", failedRequest);

    const retryRequest = claimDetailRequest(owners, "case-1");
    expect(retryRequest).not.toBeNull();
    if (retryRequest === null) throw new Error("expected retry request token");
    expect(shouldShowDetailLoading(undefined, undefined, true)).toBe(true);

    expect(ownsDetailRequest(owners, "case-1", failedRequest)).toBe(false);
    releaseDetailRequest(owners, "case-1", failedRequest);
    expect(owners.get("case-1")).toBe(retryRequest);
    expect(shouldShowDetailLoading({ loaded: true }, undefined, false)).toBe(false);
  });

  it("coalesces an expansion with an automatic preload and cleans up its controller", () => {
    const owners = new Map<string, symbol>();
    const controllers = new Map<string, AbortController>();
    const automaticController = new AbortController();
    const automaticRequest = claimAbortableDetailRequest(
      owners,
      controllers,
      "case-1",
      automaticController,
    );
    expect(automaticRequest).not.toBeNull();
    if (automaticRequest === null) throw new Error("expected automatic request");

    const clickRequest = claimAbortableDetailRequest(
      owners,
      controllers,
      "case-1",
      new AbortController(),
    );
    expect(clickRequest).toBeNull();
    expect(controllers.size).toBe(1);

    automaticController.abort();
    releaseAbortableDetailRequest(owners, controllers, "case-1", automaticRequest);
    expect(automaticController.signal.aborted).toBe(true);
    expect(owners.has("case-1")).toBe(false);
    expect(controllers.has("case-1")).toBe(false);
  });
});
