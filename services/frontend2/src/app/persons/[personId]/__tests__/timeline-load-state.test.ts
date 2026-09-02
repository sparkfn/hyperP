import { describe, expect, it } from "vitest";
import { timelineLoadState } from "../timeline-load-state";

describe("timeline deferred load state", () => {
  it("marks partial and rejected resource results incomplete", () => {
    expect(timelineLoadState([{ status: "fulfilled", value: [] }, { status: "rejected", reason: new Error("offline") }])).toBe("incomplete");
  });
  it("marks only fully fulfilled results complete", () => {
    expect(timelineLoadState([{ status: "fulfilled", value: [] }, { status: "fulfilled", value: [] }])).toBe("complete");
  });
});
