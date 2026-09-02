import { describe, expect, it } from "vitest";

import { sidebarResourceState } from "../sidebar-resource-state";

describe("sidebar deferred resource state", () => {
  it("renders unavailable after a rejected request instead of an authoritative empty state", () => {
    expect(sidebarResourceState({ status: "rejected", reason: new Error("offline") })).toBe("unavailable");
    expect(sidebarResourceState({ status: "fulfilled", value: [] as string[] })).toBe("ready");
  });
});
