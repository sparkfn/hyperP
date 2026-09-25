// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Person } from "@/lib/api-types";
import type { PersonSourceRecord } from "@/lib/api-types-person";
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

function sourceRecordFact(
  sourceRecordPk: string,
  fullName: string,
  observedAt: string,
): PersonSourceRecord {
  return {
    source_record_pk: sourceRecordPk,
    source_system: "fundbox",
    entity_key: null,
    entity_display_name: null,
    source_record_id: sourceRecordPk,
    source_record_version: null,
    record_type: "identity",
    lifecycle_status: "active",
    extraction_method: null,
    extraction_confidence: null,
    link_status: "linked",
    linked_person_id: null,
    observed_at: observedAt,
    ingested_at: observedAt,
    normalized_payload: { attributes: [{ attribute_name: "full_name", attribute_value: fullName }] },
    raw_payload: null,
    conversation_ref: null,
    observed_at_display: "20 Aug 2026",
    ingested_at_display: "20 Aug 2026",
    extraction_confidence_display: null,
    chat_transcript: null,
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

describe("ReviewActionsPanel merge choices", () => {
  it("deduplicates corroborating records and submits the winning source record", async () => {
    vi.mocked(bffFetch).mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return { review_case_id: "case-1", queue_state: "resolved", resolution: "merge" };
      }
      if (path.includes("/source-records")) {
        return path.includes("person-a")
          ? [
              sourceRecordFact("sr-a1", "Alice Tan", "2026-08-20T00:00:00Z"),
              sourceRecordFact("sr-a2", "Alice Tan", "2026-08-20T00:05:00Z"),
            ]
          : [sourceRecordFact("sr-b1", "Alice Tan", "2026-08-20T00:01:00Z")];
      }
      if (path.includes("/identifiers")) return [];
      if (path.endsWith("person-a")) return person("person-a", "Alice Tan", "+6511111111");
      if (path.endsWith("person-b")) return person("person-b", "Alice Tan", "+6522222222");
      throw new Error(`Unexpected path: ${path}`);
    });
    const user = userEvent.setup();
    const onActionDone = vi.fn();

    vi.stubGlobal("React", React);
    render(
      <ReviewActionsPanel
        reviewCaseId="case-1"
        queueState="open"
        assignedTo={null}
        leftPersonId="person-a"
        rightPersonId="person-b"
        onChanged={async () => undefined}
        onActionDone={onActionDone}
      />,
    );

    const aliceRadios = await screen.findAllByLabelText("Alice Tan");
    expect(aliceRadios).toHaveLength(2);
    const leftRadio = aliceRadios[0];
    const rightRadio = aliceRadios[1];
    if (leftRadio === undefined || rightRadio === undefined) throw new Error("radios not found");
    const leftStack = leftRadio.parentElement?.parentElement ?? null;
    const rightStack = rightRadio.parentElement?.parentElement ?? null;
    if (leftStack === null || rightStack === null) throw new Error("choice stacks not found");
    expect(leftStack === rightStack).toBe(false);
    expect(within(leftStack).getAllByRole("radio")).toHaveLength(1);
    expect(within(rightStack).getAllByRole("radio")).toHaveLength(1);

    await user.click(leftRadio);
    await user.selectOptions(
      screen.getByLabelText("Review note"),
      screen.getByRole("option", { name: /matching government ID and name/ }),
    );
    await user.click(screen.getByRole("button", { name: "Submit action" }));

    await waitFor(() =>
      expect(onActionDone).toHaveBeenCalledWith(true, expect.stringContaining("Merged")),
    );
    expect(screen.getByText("Merged — state: resolved (merge).")).toBeTruthy();

    const postCall = vi
      .mocked(bffFetch)
      .mock.calls.find(([path]) => String(path).includes("/review-cases/case-1/actions"));
    const postedBody = postCall?.[1]?.body;
    expect(typeof postedBody).toBe("string");
    const body: unknown = JSON.parse(String(postedBody));
    expect(body).toEqual({
      action_type: "merge",
      notes: "Confirmed same person — matching government ID and name.",
      metadata: {
        follow_up_at: null,
        survivor_person_id: "person-b",
        golden_profile_selections: [
          {
            field_name: "preferred_full_name",
            source_kind: "source_record_fact",
            selected_value: "Alice Tan",
            source_record_pk: "sr-a2",
            identifier_type: null,
          },
        ],
      },
    });
  });
});
