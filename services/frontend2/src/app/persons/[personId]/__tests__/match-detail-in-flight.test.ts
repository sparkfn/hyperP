import { describe, expect, it } from "vitest";

import {
  claimDetailRequest,
  isAbortError,
  needsDetail,
  releaseDetailGeneration,
  releaseDetailRequest,
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
});
