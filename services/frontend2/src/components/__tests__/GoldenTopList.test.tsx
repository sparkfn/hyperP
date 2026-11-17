// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import React from "react";
import { describe, expect, it } from "vitest";

import type { GoldenTopValue } from "@/lib/api-types";
import { GoldenTopList } from "../GoldenTopList";

function entry(overrides: Partial<GoldenTopValue> = {}): GoldenTopValue {
  return {
    value: "+6591234567",
    rank: 2,
    score: 0.95,
    verified: false,
    last_confirmed_at: "2026-02-01T00:00:00Z",
    source_count: 3,
    ...overrides,
  };
}

describe("GoldenTopList", () => {
  it("renders values, ranks, verification, score, and source count", () => {
    render(
      <GoldenTopList
        label="Phone"
        entries={[
          entry(),
          entry({
            value: "+6597654321",
            rank: 3,
            score: 0.6,
            verified: true,
            source_count: 1,
          }),
        ]}
      />,
    );

    expect(screen.getByText("Phone runners-up")).toBeTruthy();
    expect(screen.getByText("#2 +6591234567")).toBeTruthy();
    expect(screen.getByText("#3 +6597654321")).toBeTruthy();
    expect(screen.getByText("Verified")).toBeTruthy();
    expect(screen.getByText("score 0.95 · 3 sources · 2026-02-01T00:00:00Z")).toBeTruthy();
    expect(screen.getByText("score 0.6 · 1 sources · 2026-02-01T00:00:00Z")).toBeTruthy();
  });

  it("omits the evidence timestamp when the entry has none", () => {
    render(<GoldenTopList label="Email" entries={[entry({ last_confirmed_at: null })]} />);

    expect(screen.getByText("score 0.95 · 3 sources")).toBeTruthy();
  });

  it("renders nothing for an empty block", () => {
    const { container } = render(<GoldenTopList label="Phone" entries={[]} />);

    expect(container.innerHTML).toBe("");
  });
});
