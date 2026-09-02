import { describe, expect, it } from "vitest";
import { retainOnFailure, timelineLoadState } from "../timeline-load-state";

describe("timeline deferred load state", () => {
  it("marks partial and rejected resource results incomplete", () => {
    expect(timelineLoadState([{ status: "fulfilled", value: [] }, { status: "rejected", reason: new Error("offline") }])).toBe("incomplete");
  });
  it("marks only fully fulfilled results complete", () => {
    expect(timelineLoadState([{ status: "fulfilled", value: [] }, { status: "fulfilled", value: [] }])).toBe("complete");
  });
  it("retains an earlier successful portion when a retry fails", () => {
    expect(retainOnFailure(["earlier"], { status: "rejected", reason: new Error("retry") }))
      .toEqual(["earlier"]);
    expect(retainOnFailure(["earlier"], { status: "fulfilled", value: { data: ["new"] } }))
      .toEqual(["new"]);
  });
});
