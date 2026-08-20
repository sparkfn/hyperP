import { describe, expect, it } from "vitest";

import { formatPaginationShowing, hasEffectivePersonListFilters } from "../person-list-view";

describe("hasEffectivePersonListFilters", () => {
  it("ignores pagination, sorting, and count-control parameters", () => {
    expect(hasEffectivePersonListFilters(
      "limit=25&sort_by=profile_completeness_score&sort_order=desc&include_total=false",
    )).toBe(false);
  });

  it.each([
    "q=alice&limit=25",
    "entity_key=fundbox&limit=25",
    "source_key=crm&limit=25",
    "has_system_match=true&limit=25",
    "has_bankruptcy_case=true&limit=25",
    "addr_city=Manila&limit=25",
  ])("recognizes an effective filter in %s", (query) => {
    expect(hasEffectivePersonListFilters(query)).toBe(true);
  });
});

describe("formatPaginationShowing", () => {
  it("uses the summary total only for an unfiltered list", () => {
    expect(formatPaginationShowing({
      rowCount: 25,
      pageIndex: 0,
      pageSize: 25,
      exactTotal: null,
      unfilteredSummaryTotal: 164_188,
      hasEffectiveFilters: false,
    })).toBe("Showing 1\u201325 of 164,188 profiles");
  });

  it("does not present the unfiltered summary as a filtered total", () => {
    expect(formatPaginationShowing({
      rowCount: 8,
      pageIndex: 0,
      pageSize: 25,
      exactTotal: null,
      unfilteredSummaryTotal: 164_188,
      hasEffectiveFilters: true,
    })).toBe("Showing 1\u20138 profiles");
  });

  it("prefers an exact total when the API supplies one", () => {
    expect(formatPaginationShowing({
      rowCount: 25,
      pageIndex: 1,
      pageSize: 25,
      exactTotal: 52,
      unfilteredSummaryTotal: 164_188,
      hasEffectiveFilters: true,
    })).toBe("Showing 26\u201350 of 52 profiles");
  });

  it("suppresses a stale total that is below the current page end", () => {
    expect(formatPaginationShowing({
      rowCount: 25,
      pageIndex: 1,
      pageSize: 25,
      exactTotal: null,
      unfilteredSummaryTotal: 25,
      hasEffectiveFilters: false,
    })).toBe("Showing 26\u201350 profiles");
  });

  it("shows a zero range for an empty page", () => {
    expect(formatPaginationShowing({
      rowCount: 0,
      pageIndex: 1,
      pageSize: 25,
      exactTotal: null,
      unfilteredSummaryTotal: null,
      hasEffectiveFilters: true,
    })).toBe("Showing 0\u20130 profiles");
  });
});
