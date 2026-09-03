import { describe, expect, it } from "vitest";
import { metricsByEntity } from "../entity-metrics-state";

describe("entity metric state", () => {
  it("does not fabricate metrics for metadata without an exact metric response", () => {
    expect(metricsByEntity([]).get("eko")).toBeUndefined();
  });
});
