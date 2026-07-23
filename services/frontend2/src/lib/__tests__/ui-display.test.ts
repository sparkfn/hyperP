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
  it.each([
    ["fundbox-user-123", "fundbox", "user-123"],
    ["fundbox-contact-456", "fundbox:contacts", "contact-456"],
    ["fundbox-legacy-7", "fundbox:legacy", "legacy-7"],
    ["fundbox-merged-8", "fundbox:merged", "merged-8"],
    ["fundbox-order-9", "fundbox:sales", "order-9"],
    ["speedzone_phppos-customer-789", "speedzone_phppos", "customer-789"],
    ["speedzone_phppos-sale-10", "speedzone_phppos:sales", "sale-10"],
    ["eko_phppos-person-101", "eko_phppos", "person-101"],
    ["eko_phppos-customer-102", "eko_phppos", "customer-102"],
    ["whatsapp-chat-42-person-0", "whatsapp_chat", "chat-42-person-0"],
    ["bitrix-chat-55-person-1", "bitrix_chat", "chat-55-person-1"],
    ["onediver-profile-12", "onediver", "profile-12"],
    ["onediver-emergency-12-1", "onediver", "emergency-12-1"],
    ["onediver-salesorder-44", "onediver:sales", "salesorder-44"],
  ])("removes the known prefix from %s", (value, sourceSystem, expected) => {
    expect(sourceRecordReference(value, sourceSystem)).toBe(expected);
  });

  it.each([
    ["fundbox-user-123", "speedzone_phppos"],
    ["unknown-prefix-record-123", "unknown_source"],
    ["bankruptcy_case:1", "sgbankruptcy"],
    ["rental_flat:33", "sgrentalflats"],
    ["SPZ-CUST-00123", "speedzone_phppos"],
    ["fundbox", "fundbox"],
  ])("leaves ambiguous or non-matching value %s unchanged", (value, sourceSystem) => {
    expect(sourceRecordReference(value, sourceSystem)).toBe(value);
  });

  it.each([null, undefined, ""])("handles empty value %s", (value) => {
    expect(sourceRecordReference(value, "fundbox")).toBe(value ?? "");
  });

  it("requires a matching source system before removing a prefix", () => {
    expect(sourceRecordReference("bitrix-chat-55-person-1", "bitrix_chat")).toBe("chat-55-person-1");
    expect(sourceRecordReference("bitrix-chat-55-person-1", "unknown_source")).toBe("bitrix-chat-55-person-1");
  });
});
