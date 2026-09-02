import { describe, expect, it } from "vitest";

import type { PersonIdentifier } from "@/lib/api-types-person";
import { mergeIdentifierPage } from "../person-detail-data";

function identifier(value: number): PersonIdentifier {
  return {
    identifier_type: "phone",
    normalized_value: `+650000${value.toString().padStart(4, "0")}`,
    is_active: true,
    is_verified: true,
    last_confirmed_at: null,
    source_system_key: "pos",
    source_record_ids: [],
    entities: [],
    source_records: [],
  };
}

describe("identifier cursor pages", () => {
  it("keeps every identifier reachable across bounded pages", () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => identifier(index + 1));
    const secondPage = [identifier(50), ...Array.from({ length: 50 }, (_, index) => identifier(index + 51))];

    const merged = mergeIdentifierPage(firstPage, secondPage);

    expect(merged).toHaveLength(100);
    expect(merged.map((item) => item.normalized_value)).toEqual(
      Array.from({ length: 100 }, (_, index) => identifier(index + 1).normalized_value),
    );
  });
});
