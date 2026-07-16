import { describe, expect, it } from "vitest";

import { createHyperPQueryClient } from "../query-client";

describe("createHyperPQueryClient", () => {
  it("preserves current no-retry and no-focus-refetch behavior", () => {
    const client = createHyperPQueryClient();
    const defaults = client.getDefaultOptions();

    expect(defaults.queries?.retry).toBe(false);
    expect(defaults.queries?.refetchOnWindowFocus).toBe(false);
    expect(defaults.mutations?.retry).toBe(false);
  });

  it("keeps cached query data long enough for client navigation reuse", () => {
    const client = createHyperPQueryClient();

    expect(client.getDefaultOptions().queries?.gcTime).toBe(5 * 60 * 1000);
  });
});
