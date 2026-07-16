import { describe, expect, it } from "vitest";

import { personDisplayName, shortReference, sourceRecordReference } from "../ui-display";

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

describe("sourceRecordReference", () => {
  it("removes a known redundant source prefix", () => {
    expect(sourceRecordReference("BITRIX-CONTACT-8821")).toBe("CONTACT-8821");
    expect(sourceRecordReference("SPZ-CUST-00123")).toBe("CUST-00123");
  });

  it("preserves references with formats that cannot be safely shortened", () => {
    expect(sourceRecordReference("WA-2026-05-001")).toBe("WA-2026-05-001");
    expect(sourceRecordReference("WA-2027-05-001")).toBe("WA-2027-05-001");
    expect(sourceRecordReference("OTHER-CONTACT-8821")).toBe("OTHER-CONTACT-8821");
    expect(sourceRecordReference("SPZ-00123")).toBe("SPZ-00123");
    expect(sourceRecordReference("00123")).toBe("00123");
  });
});
