import { describe, expect, it } from "vitest";

import { uniqueChoices, type GoldenProfileChoice } from "../golden-profile-choices";

function choice(overrides: Partial<GoldenProfileChoice> & Pick<GoldenProfileChoice, "key">): GoldenProfileChoice {
  return {
    fieldName: "preferred_full_name",
    label: "Full name",
    value: "Alice Tan",
    displayValue: "Alice Tan",
    sourceKind: "source_record_fact",
    sourceRecordPk: "sr-1",
    identifierType: null,
    sourceLabel: "fundbox",
    personId: "person-a",
    isSurvivorDefault: false,
    observedAt: "2026-08-20T00:00:00Z",
    ...overrides,
  };
}

describe("uniqueChoices", () => {
  it("collapses case and whitespace variants of the same person, field, and value", () => {
    const collapsed = uniqueChoices([
      choice({ key: "a", value: "Alice Tan", sourceRecordPk: "sr-1" }),
      choice({ key: "b", value: "  alice tan  ", sourceRecordPk: "sr-2" }),
    ]);

    expect(collapsed).toHaveLength(1);
    expect(collapsed[0]?.value).toBe("Alice Tan");
    expect(collapsed[0]?.sourceRecordPk).toBe("sr-1");
  });

  it("prefers a corroborated source-record fact over an uncorroborated one", () => {
    const collapsed = uniqueChoices([
      choice({ key: "a", sourceRecordPk: null, observedAt: "2026-08-21T00:00:00Z" }),
      choice({ key: "b", sourceRecordPk: "sr-2", observedAt: "2026-08-20T00:00:00Z" }),
    ]);

    expect(collapsed).toHaveLength(1);
    expect(collapsed[0]?.sourceRecordPk).toBe("sr-2");
  });

  it("prefers a source-record fact over a newer identifier for the same value", () => {
    const fact = choice({
      key: "fact",
      fieldName: "preferred_phone",
      value: "+6591234567",
      sourceKind: "source_record_fact",
      sourceRecordPk: "sr-1",
      observedAt: "2026-08-20T00:00:00Z",
    });
    const newerIdentifier = choice({
      key: "identifier",
      fieldName: "preferred_phone",
      value: "+6591234567",
      sourceKind: "identifier",
      identifierType: "phone",
      sourceRecordPk: "sr-9",
      observedAt: "2026-08-21T00:00:00Z",
    });

    expect(uniqueChoices([newerIdentifier, fact])[0]?.sourceKind).toBe("source_record_fact");
    expect(uniqueChoices([fact, newerIdentifier])[0]?.sourceKind).toBe("source_record_fact");
    expect(uniqueChoices([fact, newerIdentifier])[0]?.sourceRecordPk).toBe("sr-1");
  });

  it("keeps the most recently observed value regardless of input order", () => {
    const older = choice({ key: "a", sourceRecordPk: "sr-1", observedAt: "2026-08-20T00:00:00Z" });
    const newer = choice({ key: "b", sourceRecordPk: "sr-2", observedAt: "2026-08-20T00:05:00Z" });

    expect(uniqueChoices([older, newer])[0]?.sourceRecordPk).toBe("sr-2");
    expect(uniqueChoices([newer, older])[0]?.sourceRecordPk).toBe("sr-2");
  });

  it("ranks offset ISO instants chronologically, not lexically", () => {
    // 09:00+08:00 is 01:00Z, i.e. lexically greater but chronologically earlier
    // than 02:00Z.
    const offsetEarlier = choice({
      key: "offset",
      sourceRecordPk: "sr-a",
      observedAt: "2026-08-20T09:00:00+08:00",
    });
    const utcLater = choice({
      key: "utc",
      sourceRecordPk: "sr-b",
      observedAt: "2026-08-20T02:00:00Z",
    });

    expect(uniqueChoices([offsetEarlier, utcLater])[0]?.sourceRecordPk).toBe("sr-b");
    expect(uniqueChoices([utcLater, offsetEarlier])[0]?.sourceRecordPk).toBe("sr-b");
  });

  it("ranks empty or invalid timestamps below parsed instants", () => {
    const invalid = choice({ key: "a", sourceRecordPk: "sr-1", observedAt: "" });
    const parsed = choice({ key: "b", sourceRecordPk: "sr-2", observedAt: "2026-08-20T00:00:00Z" });

    expect(uniqueChoices([invalid, parsed])[0]?.sourceRecordPk).toBe("sr-2");
    expect(uniqueChoices([parsed, invalid])[0]?.sourceRecordPk).toBe("sr-2");
  });

  it("breaks ties on PK and time with the deterministic choice key", () => {
    const keyB = choice({
      key: "b",
      sourceKind: "identifier",
      identifierType: "phone",
      fieldName: "preferred_phone",
      sourceRecordPk: "sr-1",
      observedAt: "2026-08-20T00:00:00Z",
    });
    const keyA = choice({
      key: "a",
      sourceKind: "identifier",
      identifierType: "phone",
      fieldName: "preferred_phone",
      sourceRecordPk: "sr-1",
      observedAt: "2026-08-20T00:00:00Z",
    });

    expect(uniqueChoices([keyB, keyA])[0]?.key).toBe("a");
    expect(uniqueChoices([keyA, keyB])[0]?.key).toBe("a");
  });

  it("keeps an identical raw value and PK on different persons separate", () => {
    const collapsed = uniqueChoices([
      choice({ key: "a", personId: "person-a", sourceRecordPk: "sr-1", value: "Alice Tan" }),
      choice({ key: "b", personId: "person-b", sourceRecordPk: "sr-1", value: "Alice Tan" }),
    ]);

    expect(collapsed).toHaveLength(2);
    expect(collapsed.map((item) => item.personId)).toEqual(["person-a", "person-b"]);
  });

  it("keeps distinct values and distinct fields separate", () => {
    const collapsed = uniqueChoices([
      choice({ key: "a", fieldName: "preferred_phone", value: "+6511111111" }),
      choice({ key: "b", fieldName: "preferred_phone", value: "+6522222222" }),
      choice({ key: "c", fieldName: "preferred_email", value: "+6511111111" }),
    ]);

    expect(collapsed).toHaveLength(3);
  });
});
