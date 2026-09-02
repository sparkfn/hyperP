// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Person } from "@/lib/api-types";
import ReviewActionsPanel from "../ReviewActionsPanel";
import { bffFetch } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({
  BffError: class BffError extends Error {},
  bffFetch: vi.fn(),
}));

function person(personId: string, name: string, phone: string): Person {
  return {
    person_id: personId,
    status: "active",
    is_high_value: false,
    is_high_risk: false,
    preferred_full_name: name,
    preferred_phone: phone,
    preferred_email: `${personId}@example.com`,
    preferred_dob: "1990-01-02",
    preferred_address: null,
    preferred_nric: null,
    preferred_race_ethnicity: null,
    profile_completeness_score: 0.8,
    golden_profile_computed_at: null,
    golden_profile_version: null,
    source_record_count: 3,
    connection_count: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function renderPanel(): void {
  vi.stubGlobal("React", React);
  render(
    <ReviewActionsPanel
      reviewCaseId="case-1"
      queueState="open"
      assignedTo={null}
      leftPersonId={null}
      rightPersonId="person-a"
      reviewCandidatePersonIds={["person-a", "person-b"]}
      onChanged={async () => undefined}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("ReviewActionsPanel CRM candidates", () => {
  it("loads named candidate summaries and updates the inspected profile on selection", async () => {
    vi.mocked(bffFetch).mockImplementation(async (path: string) => {
      if (path.endsWith("person-a")) return person("person-a", "Person Alpha", "+6511111111");
      if (path.endsWith("person-b")) return person("person-b", "Person Beta", "+6522222222");
      throw new Error(`Unexpected path: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel();

    const selector = await screen.findByLabelText("CRM owner candidate");
    expect(await screen.findByRole("option", { name: "Person Alpha - person-a" })).toBeTruthy();
    expect(await screen.findByRole("option", { name: "Person Beta - person-b" })).toBeTruthy();
    await user.selectOptions(selector, "person-b");

    await waitFor(() => {
      expect(screen.getByText("Phone: +6522222222")).toBeTruthy();
      expect(screen.getByText("Person ID: person-b")).toBeTruthy();
    });
  });

  it("keeps merge submission disabled until the selected profile is inspectable", async () => {
    vi.mocked(bffFetch).mockImplementation(
      async () => new Promise<Person>(() => undefined),
    );
    const user = userEvent.setup();

    renderPanel();
    await user.selectOptions(
      screen.getByLabelText("Review note"),
      screen.getByRole("option", { name: /matching government ID and name/ }),
    );

    expect(
      screen.getByRole("button", { name: "Submit action" }).hasAttribute("disabled"),
    ).toBe(true);
  });
});
