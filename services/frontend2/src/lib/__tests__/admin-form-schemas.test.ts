import { describe, expect, it } from "vitest";

import { oauthClientFormSchema } from "../admin-form-schemas";

describe("oauthClientFormSchema", () => {
  it("trims a valid client name and accepts a bounded TTL", () => {
    const result = oauthClientFormSchema.parse({
      name: "  Data Pipeline  ",
      scopes: ["persons:read"],
      ttlMinutes: "15",
    });

    expect(result).toEqual({
      name: "Data Pipeline",
      scopes: ["persons:read"],
      ttlMinutes: "15",
    });
  });

  it.each(["4", "1441", "not-a-number"]) (
    "rejects invalid TTL value %s",
    (ttlMinutes) => {
      expect(oauthClientFormSchema.safeParse({
        name: "Data Pipeline",
        scopes: ["persons:read"],
        ttlMinutes,
      }).success).toBe(false);
    },
  );

  it("requires at least one known scope", () => {
    expect(oauthClientFormSchema.safeParse({
      name: "Data Pipeline",
      scopes: [],
      ttlMinutes: "15",
    }).success).toBe(false);
  });
});
