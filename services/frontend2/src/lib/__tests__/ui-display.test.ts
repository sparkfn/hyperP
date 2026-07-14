import { describe, expect, it } from "vitest";

import { personDisplayName, shortReference } from "../ui-display";

describe("personDisplayName", () => {
  it("uses a person's display name without exposing the internal id", () => {
    expect(personDisplayName("Ada Lovelace")).toBe("Ada Lovelace");
  });

  it("uses a human fallback when a display name is missing", () => {
    expect(personDisplayName(null)).toBe("Unnamed person");
  });
});

describe("shortReference", () => {
  it("labels and shortens a long internal reference", () => {
    expect(shortReference("1234567890abcdef", "Case ref")).toBe("Case ref: 12345678…");
  });

  it("keeps a short reference intact", () => {
    expect(shortReference("abc123", "Case ref")).toBe("Case ref: abc123");
  });
});
