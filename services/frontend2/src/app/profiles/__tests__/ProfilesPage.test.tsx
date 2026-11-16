// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import React from "react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { usePaginatedFetch } = vi.hoisted(() => ({ usePaginatedFetch: vi.fn() }));

vi.mock("@/lib/usePaginatedFetch", () => ({ usePaginatedFetch }));
vi.mock("next/link", () => ({
  default: ({ children, href }: { children: ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

import ProfilesPage from "../page";

afterEach(cleanup);

describe("ProfilesPage", () => {
  it("always marks rows unverified and defaults to open profiles", () => {
    usePaginatedFetch.mockReturnValue({
      rows: [
        {
          profile_id: "profile-1",
          status: "open",
          evidence_version: 1,
          materialized_at: null,
          promoted_person_id: null,
          record_count: 2,
          identifier_count: 1,
        },
      ],
      error: null,
      loading: false,
      from: 1,
      to: 1,
      total: 1,
      hasPrev: false,
      hasNext: false,
      goNext: vi.fn(),
      goPrev: vi.fn(),
    });

    render(<ProfilesPage />);

    expect(usePaginatedFetch).toHaveBeenCalledWith("/bff/profiles?status=open");
    expect(screen.getByText("Unverified")).toBeTruthy();
    expect(screen.getByText("profile-1").closest("a")?.getAttribute("href")).toBe(
      "/profiles/profile-1",
    );
  });
});
