import { describe, expect, it } from "vitest";

import {
  applyCrmDealRange,
  crmDealPreset,
  crmDealRangeLabel,
  isValidCrmDealRange,
  parseCrmDealRange,
} from "../person-list-crm-controls";
import {
  PERSON_LIST_COLUMNS,
  PERSON_LIST_DEFAULT_WIDTHS,
  PERSON_LIST_TABLE_COLUMN_COUNT,
} from "../person-list-columns";

describe("person-list CRM deal controls", () => {
  it("maps the three presets to the API range contract", () => {
    expect(crmDealPreset({ min: "", max: "" })).toBe("any");
    expect(crmDealPreset({ min: "1", max: "" })).toBe("has");
    expect(crmDealPreset({ min: "0", max: "0" })).toBe("none");
  });

  it("preserves invalid values for the API to reject", () => {
    const range = { min: "3", max: "2" };
    const params = new URLSearchParams();

    expect(isValidCrmDealRange(range)).toBe(false);
    expect(crmDealRangeLabel(range)).toBe("CRM Deals: invalid range");
    applyCrmDealRange(params, range);
    expect(params.toString()).toBe("crm_deal_count_min=3&crm_deal_count_max=2");
  });

  it("omits empty bounds and round-trips explicit zero", () => {
    const params = new URLSearchParams("crm_deal_count_max=0");
    expect(parseCrmDealRange(params)).toEqual({ min: "", max: "0" });

    applyCrmDealRange(params, { min: "", max: "" });
    expect(params.toString()).toBe("");
  });

  it("marks negative and fractional values invalid", () => {
    expect(isValidCrmDealRange({ min: "-1", max: "" })).toBe(false);
    expect(isValidCrmDealRange({ min: "1.5", max: "" })).toBe(false);
  });
});


describe("person-list column metadata", () => {
  it("keeps widths and rendered column count aligned", () => {
    expect(PERSON_LIST_COLUMNS.map((column) => column.key)).toContain("deals");
    expect(PERSON_LIST_DEFAULT_WIDTHS).toHaveLength(PERSON_LIST_COLUMNS.length);
    expect(PERSON_LIST_TABLE_COLUMN_COUNT).toBe(PERSON_LIST_COLUMNS.length);
  });
});
