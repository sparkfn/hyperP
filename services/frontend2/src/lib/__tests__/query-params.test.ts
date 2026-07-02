import { describe, expect, it } from "vitest";

import { appendQueryParams, searchParamsToQuery } from "../query-params";

describe("searchParamsToQuery", () => {
  it("collects repeated keys into an array (regression: BFF must not collapse multi-value filters)", () => {
    const sp = new URLSearchParams("entity_key=fundbox&entity_key=onediver&entity_key_mode=and");
    expect(searchParamsToQuery(sp)).toEqual({
      entity_key: ["fundbox", "onediver"],
      entity_key_mode: "and",
    });
  });

  it("returns a scalar string for non-repeated keys", () => {
    expect(searchParamsToQuery(new URLSearchParams("q=abc&limit=25"))).toEqual({
      q: "abc",
      limit: "25",
    });
  });

  it("preserves first-seen key order regardless of repetition", () => {
    const sp = new URLSearchParams("a=1&b=2&a=3");
    expect(Object.keys(searchParamsToQuery(sp))).toEqual(["a", "b"]);
  });
});

describe("appendQueryParams", () => {
  it("serializes an array as repeated params so multi-value filters reach the API", () => {
    const url = new URL("http://x/api/persons");
    appendQueryParams(url, {
      entity_key: ["fundbox", "onediver"],
      entity_key_mode: "and",
      q: undefined,
      skip: null,
    });
    expect(url.searchParams.getAll("entity_key")).toEqual(["fundbox", "onediver"]);
    expect(url.searchParams.get("entity_key_mode")).toBe("and");
    // null/undefined must be dropped, not stringified.
    expect(url.searchParams.has("q")).toBe(false);
    expect(url.searchParams.has("skip")).toBe(false);
  });

  it("coerces numeric/boolean scalars to strings", () => {
    const url = new URL("http://x/api/persons");
    appendQueryParams(url, { limit: 25, is_high_value: true });
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.get("is_high_value")).toBe("true");
  });

  it("round-trips a BFF query through parse -> URL without losing values", () => {
    // This is the exact regression: pre-fix the BFF forwarded only
    // `?entity_key=onediver&entity_key_mode=and` (fundbox dropped), so the API
    // saw a single-key list and AND behaved identically to OR.
    const sp = new URLSearchParams("entity_key=fundbox&entity_key=onediver&entity_key_mode=and");
    const url = new URL("http://x/api/persons");
    appendQueryParams(url, searchParamsToQuery(sp));
    expect(url.search).toBe("?entity_key=fundbox&entity_key=onediver&entity_key_mode=and");
  });
});
