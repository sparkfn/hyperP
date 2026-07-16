import { describe, expect, it } from "vitest";

import { parseGoogleRefreshResponse, parseMeResponse } from "../auth-contracts";

describe("parseGoogleRefreshResponse", () => {
  it("accepts the required Google token response", () => {
    expect(parseGoogleRefreshResponse({
      id_token: "id-token",
      access_token: "access-token",
      expires_in: 3600,
    })).toEqual({
      id_token: "id-token",
      access_token: "access-token",
      expires_in: 3600,
    });
  });

  it("rejects missing or invalid token fields", () => {
    expect(parseGoogleRefreshResponse({ access_token: "token", expires_in: -1 })).toBeNull();
  });
});

describe("parseMeResponse", () => {
  it("accepts known role and nullable entity fields", () => {
    const response = {
      data: {
        email: "user@example.com",
        google_sub: "google-sub",
        role: "employee",
        entity_key: null,
        display_name: "Example User",
      },
    };

    expect(parseMeResponse(response)).toEqual(response);
  });

  it("rejects an unknown role", () => {
    expect(parseMeResponse({
      data: {
        email: "user@example.com",
        google_sub: "google-sub",
        role: "superuser",
        entity_key: null,
        display_name: null,
      },
    })).toBeNull();
  });
});
