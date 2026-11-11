// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Person } from "@/lib/api-types";
import type { PersonSourceRecord } from "@/lib/api-types-person";
import type { BulkReviewActionItemResult } from "@/lib/api-types-ops";
import ReviewActionsPanel, { BulkReviewActionsPanel } from "../ReviewActionsPanel";
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

describe("ReviewActionsPanel pair-audit guard", () => {
  it("starts on reject and never loads merge choices for a pair-audit case", async () => {
    vi.mocked(bffFetch).mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return { review_case_id: "case-1", queue_state: "resolved", resolution: "reject" };
      }
      throw new Error(`Unexpected path: ${path}`);
    });
    const user = userEvent.setup();

    render(
      <ReviewActionsPanel
        reviewCaseId="case-1"
        queueState="open"
        assignedTo={null}
        leftPersonId="person-a"
        rightPersonId="person-b"
        engineType="pair_audit"
        onChanged={async () => undefined}
      />,
    );

    const actionSelect = screen.getByLabelText("Action type") as HTMLSelectElement;
    await waitFor(() => expect(actionSelect.value).toBe("reject"));
    const mergeOption = within(actionSelect).getByRole("option", { name: "Merge" }) as HTMLOptionElement;
    expect(mergeOption.disabled).toBe(true);
    expect(screen.getByText("Merge is disabled for pair-audit cases.")).toBeTruthy();

    await user.selectOptions(
      screen.getByLabelText("Review note"),
      screen.getByRole("option", { name: "Different people — conflicting identity details." }),
    );
    await user.click(screen.getByRole("button", { name: "Submit action" }));

    await waitFor(() =>
      expect(
        vi.mocked(bffFetch).mock.calls.some(([path]) => String(path).includes("/actions")),
      ).toBe(true),
    );
    const postCall = vi
      .mocked(bffFetch)
      .mock.calls.find(([path]) => String(path).includes("/review-cases/case-1/actions"));
    const body: unknown = JSON.parse(String(postCall?.[1]?.body));
    expect(body).toMatchObject({ action_type: "reject" });
  });

  it("cancels in-flight merge choices and blocks submission when the engine turns pair-audit", async () => {
    const evidenceCalls: string[] = [];
    vi.mocked(bffFetch).mockImplementation(async (path: string) => {
      if (path.includes("/source-records")) {
        evidenceCalls.push(path);
        return new Promise<PersonSourceRecord[]>(() => undefined);
      }
      if (path.includes("/identifiers")) return [];
      if (path.endsWith("person-a")) return person("person-a", "Alice Tan", "+6511111111");
      if (path.endsWith("person-b")) return person("person-b", "Alice Tan", "+6522222222");
      throw new Error(`Unexpected path: ${path}`);
    });
    const user = userEvent.setup();

    const { rerender } = render(
      <ReviewActionsPanel
        reviewCaseId="case-1"
        queueState="open"
        assignedTo={null}
        leftPersonId="person-a"
        rightPersonId="person-b"
        engineType="heuristic"
        onChanged={async () => undefined}
      />,
    );

    await waitFor(() => expect(evidenceCalls.length).toBeGreaterThan(0));
    const evidenceCallsBeforeEngineChange = evidenceCalls.length;

    rerender(
      <ReviewActionsPanel
        reviewCaseId="case-1"
        queueState="open"
        assignedTo={null}
        leftPersonId="person-a"
        rightPersonId="person-b"
        engineType="pair_audit"
        onChanged={async () => undefined}
      />,
    );

    const actionSelect = screen.getByLabelText("Action type") as HTMLSelectElement;
    await waitFor(() =>
      expect(within(actionSelect).getByRole("option", { name: "Merge" }).hasAttribute("disabled")).toBe(true),
    );
    // The mid-mount engine change must cancel the in-flight load without
    // re-triggering a golden-profile fetch.
    await waitFor(() => expect(screen.getByText("No golden profile choices available.")).toBeTruthy());
    expect(evidenceCalls.length).toBe(evidenceCallsBeforeEngineChange);

    await user.selectOptions(
      screen.getByLabelText("Review note"),
      screen.getByRole("option", { name: "Confirmed same person — matching government ID and name." }),
    );
    const submit = screen.getByRole("button", { name: "Submit action" }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(true));
    expect(
      vi.mocked(bffFetch).mock.calls.some(([path]) => String(path).includes("/actions")),
    ).toBe(false);
  });
});

describe("ReviewActionsPanel terminal queue states", () => {
  it.each(["expired", "dismissed", "superseded_by_policy"])(
    "disables reviewer actions for a %s case",
    async (queueState) => {
      vi.mocked(bffFetch).mockImplementation(async () => []);

      render(
        <ReviewActionsPanel
          reviewCaseId="case-1"
          queueState={queueState}
          assignedTo={null}
          leftPersonId={null}
          rightPersonId={null}
          onChanged={async () => undefined}
        />,
      );

      expect(
        screen.getByText(`This review case is ${queueState} and no longer accepts actions.`),
      ).toBeTruthy();
      const actionSelect = screen.getByLabelText("Action type") as HTMLSelectElement;
      expect(actionSelect.disabled).toBe(true);
      const reasonSelect = screen.getByLabelText("Review note") as HTMLSelectElement;
      expect(reasonSelect.disabled).toBe(true);
      const submit = screen.getByRole("button", { name: "Submit action" }) as HTMLButtonElement;
      expect(submit.disabled).toBe(true);
      expect(
        vi.mocked(bffFetch).mock.calls.some(([path]) => String(path).includes("/actions")),
      ).toBe(false);
    },
  );
});

function bulkOutcome(
  reviewCaseId: string,
  overrides: Partial<BulkReviewActionItemResult> = {},
): BulkReviewActionItemResult {
  return {
    review_case_id: reviewCaseId,
    status: "updated",
    queue_state: "resolved",
    resolution: "resolve",
    error_code: null,
    ...overrides,
  };
}

function renderBulk(
  props: {
    selectedReviewCaseIds: string[];
    onSelectionChange?: (remainingIds: string[]) => void;
    onCompleted?: () => Promise<void>;
    onBusyChange?: (busy: boolean) => void;
  },
): ReturnType<typeof render> {
  return render(
    <BulkReviewActionsPanel
      selectedReviewCaseIds={props.selectedReviewCaseIds}
      onSelectionChange={props.onSelectionChange ?? (() => undefined)}
      onCompleted={props.onCompleted ?? (async () => undefined)}
      onBusyChange={props.onBusyChange}
    />,
  );
}

async function applyBulk(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.selectOptions(screen.getByLabelText("Reason code"), "confirmed_match");
  await user.click(screen.getByRole("button", { name: /Apply to \d+ review case\(s\)/ }));
}

describe("BulkReviewActionsPanel", () => {
  it("reports a full success and keeps it visible after the selection clears", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [bulkOutcome("case-1"), bulkOutcome("case-2")],
    }));
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    const { rerender } = renderBulk({
      selectedReviewCaseIds: ["case-1", "case-2"],
      onSelectionChange,
    });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText("2 of 2 review case(s) updated.")).toBeTruthy(),
    );
    expect(onSelectionChange).toHaveBeenCalledWith([]);

    // The parent clears the selection; the success banner must stay visible.
    rerender(
      <BulkReviewActionsPanel
        selectedReviewCaseIds={[]}
        onSelectionChange={onSelectionChange}
        onCompleted={async () => undefined}
      />,
    );
    expect(screen.getByText("2 of 2 review case(s) updated.")).toBeTruthy();
  });

  it("keeps failed ids selected and reports the failure codes", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [
        bulkOutcome("case-1"),
        bulkOutcome("case-2", {
          status: "error",
          queue_state: null,
          resolution: null,
          error_code: "review_case_not_found",
        }),
      ],
    }));
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    renderBulk({ selectedReviewCaseIds: ["case-1", "case-2"], onSelectionChange });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/1 review case\(s\) failed \(review_case_not_found\)/)).toBeTruthy(),
    );
    expect(onSelectionChange).toHaveBeenCalledWith(["case-2"]);
  });

  it("warns about an unknown outcome and keeps the affected id selected", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [
        bulkOutcome("case-1"),
        bulkOutcome("case-2", {
          status: "error",
          queue_state: null,
          resolution: null,
          error_code: "outcome_unknown",
        }),
      ],
    }));
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    renderBulk({ selectedReviewCaseIds: ["case-1", "case-2"], onSelectionChange });

    await applyBulk(user);

    await waitFor(() =>
      expect(
        screen.getByText(/Commit outcome is unknown for 1 review case\(s\)\. The queue has been refreshed/),
      ).toBeTruthy(),
    );
    expect(onSelectionChange).toHaveBeenCalledWith(["case-2"]);
  });

  it("treats an invalid response as an unknown outcome instead of a success", async () => {
    // Only one outcome for two submitted ids: the response is incomplete.
    vi.mocked(bffFetch).mockImplementation(async () => ({ items: [bulkOutcome("case-1")] }));
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    renderBulk({ selectedReviewCaseIds: ["case-1", "case-2"], onSelectionChange });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/The bulk action response was invalid or incomplete\./)).toBeTruthy(),
    );
    // The success banner (and its dismiss button) must not be shown.
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(onSelectionChange).toHaveBeenCalledWith(["case-1", "case-2"]);
  });

  it("treats a duplicate outcome as an invalid response", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [bulkOutcome("case-1"), bulkOutcome("case-1")],
    }));
    const user = userEvent.setup();

    renderBulk({ selectedReviewCaseIds: ["case-1"] });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/The bulk action response was invalid or incomplete\./)).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  it("treats an outcome with a missing field as invalid", async () => {
    // The queue state field is absent from the outcome entirely.
    const outcomeWithoutQueueState = {
      review_case_id: "case-1",
      status: "updated",
      resolution: "resolve",
      error_code: null,
    };
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [outcomeWithoutQueueState],
    }));
    const user = userEvent.setup();

    renderBulk({ selectedReviewCaseIds: ["case-1"] });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/The bulk action response was invalid or incomplete\./)).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  it("treats a contradictory outcome as invalid", async () => {
    // A failed outcome that also names a queue state contradicts the contract.
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [
        {
          ...bulkOutcome("case-1"),
          status: "error",
          error_code: "review_case_not_found",
        },
      ],
    }));
    const user = userEvent.setup();

    renderBulk({ selectedReviewCaseIds: ["case-1"] });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/The bulk action response was invalid or incomplete\./)).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  it("treats an updated outcome with an empty queue state as invalid", async () => {
    // An empty queue state cannot describe a successful transition.
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [bulkOutcome("case-1", { queue_state: "" })],
    }));
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    renderBulk({ selectedReviewCaseIds: ["case-1"], onSelectionChange });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/The bulk action response was invalid or incomplete\./)).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(onSelectionChange).toHaveBeenCalledWith(["case-1"]);
  });

  it("keeps the retained ids in submitted order", async () => {
    // case-1 has an unknown outcome, case-2 failed: the retained selection
    // must keep the order in which the ids were submitted.
    vi.mocked(bffFetch).mockImplementation(async () => ({
      items: [
        bulkOutcome("case-1", {
          status: "error",
          queue_state: null,
          resolution: null,
          error_code: "outcome_unknown",
        }),
        bulkOutcome("case-2", {
          status: "error",
          queue_state: null,
          resolution: null,
          error_code: "review_case_not_found",
        }),
        bulkOutcome("case-3"),
      ],
    }));
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();

    renderBulk({
      selectedReviewCaseIds: ["case-1", "case-2", "case-3"],
      onSelectionChange,
    });

    await applyBulk(user);

    await waitFor(() =>
      expect(screen.getByText(/Commit outcome is unknown for 1 review case\(s\)/)).toBeTruthy(),
    );
    expect(onSelectionChange).toHaveBeenCalledWith(["case-1", "case-2"]);
  });

  it("sends a follow-up date only for a defer action", async () => {
    const bodies: unknown[] = [];
    vi.mocked(bffFetch).mockImplementation(async (path: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        const parsed: unknown = JSON.parse(String(init.body));
        bodies.push(parsed);
        return { items: [bulkOutcome("case-1")] };
      }
      throw new Error(`Unexpected path: ${path}`);
    });
    const user = userEvent.setup();

    renderBulk({ selectedReviewCaseIds: ["case-1"] });

    const applyButton = (): HTMLButtonElement =>
      screen.getByRole("button", { name: /Apply to \d+ review case\(s\)/ }) as HTMLButtonElement;

    await user.selectOptions(screen.getByLabelText("Bulk action"), "defer");
    await user.type(screen.getByLabelText("Follow-up at (ISO)"), "2026-04-15T12:00:00Z");
    await applyBulk(user);

    await waitFor(() => expect(bodies.length).toBe(1));
    expect(bodies[0]).toMatchObject({
      action_type: "defer",
      follow_up_at: "2026-04-15T12:00:00Z",
    });
    // Wait for the first action to settle before submitting a second one.
    await waitFor(() =>
      expect(screen.getByText("1 of 1 review case(s) updated.")).toBeTruthy(),
    );
    await waitFor(() => expect(applyButton().disabled).toBe(false));

    // Switching away from defer hides the date field, but the stale value
    // must not be written onto the case.
    await user.selectOptions(screen.getByLabelText("Bulk action"), "resolve");
    await applyBulk(user);

    await waitFor(() => expect(bodies.length).toBe(2));
    expect(bodies[1]).toMatchObject({ action_type: "resolve" });
    expect(bodies[1]).not.toHaveProperty("follow_up_at");
  });

  it("reports a failed queue reload instead of a false success", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => ({ items: [bulkOutcome("case-1")] }));
    const user = userEvent.setup();
    const onCompleted = vi.fn(async (): Promise<void> => {
      throw new Error("queue reload failed");
    });

    renderBulk({ selectedReviewCaseIds: ["case-1"], onCompleted });

    await applyBulk(user);

    await waitFor(() =>
      expect(
        screen.getByText(
          "1 of 1 review case(s) updated. The review queue could not be refreshed — reload it to see the current state.",
        ),
      ).toBeTruthy(),
    );
    // The success banner (and its dismiss button) must not be shown.
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(screen.getByRole("button", { name: "Reload queue" })).toBeTruthy();
  });

  it("treats a transport failure as an unknown outcome and keeps the selection", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => {
      throw new Error("network down");
    });
    const user = userEvent.setup();
    const onSelectionChange = vi.fn();
    const onCompleted = vi.fn(async () => undefined);

    renderBulk({ selectedReviewCaseIds: ["case-1", "case-2"], onSelectionChange, onCompleted });

    await applyBulk(user);

    await waitFor(() =>
      expect(
        screen.getByText(/network down\. The commit outcome for 2 submitted review case\(s\) is unknown\./),
      ).toBeTruthy(),
    );
    expect(onCompleted).toHaveBeenCalled();
    expect(onSelectionChange).toHaveBeenCalledWith(["case-1", "case-2"]);
  });

  it("blocks a retry until the queue reloads successfully", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => {
      throw new Error("network down");
    });
    const user = userEvent.setup();
    const onCompleted = vi.fn(async (): Promise<void> => {
      throw new Error("queue reload failed");
    });

    renderBulk({ selectedReviewCaseIds: ["case-1"], onCompleted });

    await applyBulk(user);

    await waitFor(() =>
      expect(
        screen.getByText(/network down\. The commit outcome for 1 submitted review case\(s\) is unknown\./),
      ).toBeTruthy(),
    );
    const submit = screen.getByRole("button", { name: /Apply to \d+ review case\(s\)/ }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(true));

    // A successful reload unblocks the retry.
    onCompleted.mockResolvedValue(undefined);
    await user.click(screen.getByRole("button", { name: "Reload queue" }));
    await waitFor(() =>
      expect(
        (screen.getByRole("button", { name: /Apply to \d+ review case\(s\)/ }) as HTMLButtonElement).disabled,
      ).toBe(false),
    );
  });

  it("locks the parent selection while a bulk action is in flight", async () => {
    vi.mocked(bffFetch).mockImplementation(async () => ({ items: [bulkOutcome("case-1")] }));
    const user = userEvent.setup();
    const onBusyChange = vi.fn();

    renderBulk({ selectedReviewCaseIds: ["case-1"], onBusyChange });

    await applyBulk(user);

    await waitFor(() => expect(onBusyChange).toHaveBeenCalledWith(true));
    await waitFor(() => expect(onBusyChange).toHaveBeenCalledWith(false));
  });
});
