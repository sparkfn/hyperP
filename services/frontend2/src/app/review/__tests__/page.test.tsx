// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React, { type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BulkReviewActionItemResult, ReviewCaseSummary } from "@/lib/api-types-ops";
import ReviewPage from "../page";

const { bffFetch, bffFetchEnvelope } = vi.hoisted(() => ({
  bffFetch: vi.fn(),
  bffFetchEnvelope: vi.fn(),
}));

vi.mock("@/lib/api-client", () => ({
  BffError: class BffError extends Error {},
  bffFetch,
  bffFetchEnvelope,
}));

// The case links point at the detail route, which is not mounted here.
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
  }: {
    href: string;
    children: ReactNode;
  }): ReactElement => <a href={href}>{children}</a>,
}));

interface QueueRequest {
  path: string;
  signal: AbortSignal | undefined;
}

interface QueueResponse {
  data: ReviewCaseSummary[];
  meta: { request_id: string; next_cursor: null; total_count: number };
}

const queueRequests: QueueRequest[] = [];

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function reviewCase(
  reviewCaseId: string,
  personId: string,
  name: string,
  queueState: string,
): ReviewCaseSummary {
  return {
    review_case_id: reviewCaseId,
    queue_state: queueState,
    priority: 90,
    assigned_to: null,
    follow_up_at: null,
    sla_due_at: null,
    resolution: null,
    resolved_at: null,
    left_person_id: personId,
    left_person_name: name,
    right_person_id: null,
    right_person_name: null,
    match_decision: {
      match_decision_id: `decision-${reviewCaseId}`,
      engine_type: "heuristic",
      decision: "review",
      confidence: 0.82,
    },
  };
}

// Filter A: the default "Open" queue.
const OPEN_ROWS: ReviewCaseSummary[] = [reviewCase("case-a", "person-alpha", "Alpha Person", "open")];
// Filter B: the "Expired" queue.
const EXPIRED_ROWS: ReviewCaseSummary[] = [
  reviewCase("case-b", "person-beta", "Beta Person", "expired"),
];

function queueResponse(rows: ReviewCaseSummary[]): QueueResponse {
  return {
    data: rows,
    meta: { request_id: "request-1", next_cursor: null, total_count: rows.length },
  };
}

function queryParam(path: string, key: string): string | null {
  return new URL(path, "http://localhost").searchParams.get(key);
}

function bulkOutcome(reviewCaseId: string): BulkReviewActionItemResult {
  return {
    review_case_id: reviewCaseId,
    status: "updated",
    queue_state: "resolved",
    resolution: "resolve",
    error_code: null,
  };
}

/** The state filter pill, reached through the clear affordance it renders. */
function stateFilterPill(): HTMLElement {
  const pill = screen.getByLabelText("Clear State filter").closest("button");
  if (pill === null) throw new Error("State filter pill not found");
  return pill;
}

function rowCheckbox(reviewCaseId: string): HTMLInputElement {
  const input = screen.getByLabelText(`Select review case ${reviewCaseId}`);
  if (!(input instanceof HTMLInputElement)) throw new Error("Review case checkbox not found");
  return input;
}

function selectAllCheckbox(): HTMLInputElement {
  const input = screen.getByLabelText("Select all review cases on this page");
  if (!(input instanceof HTMLInputElement)) throw new Error("Select-all checkbox not found");
  return input;
}

/** Start a bulk action on the selected case and leave the request in flight. */
async function startBulkAction(
  user: ReturnType<typeof userEvent.setup>,
  reviewCaseId: string,
): Promise<void> {
  await user.click(rowCheckbox(reviewCaseId));
  await user.selectOptions(screen.getByLabelText("Reason code"), "confirmed_match");
  await user.click(screen.getByRole("button", { name: /Apply to 1 review case\(s\)/ }));
}

beforeEach(() => {
  queueRequests.length = 0;
  bffFetch.mockReset();
  bffFetchEnvelope.mockReset();
  vi.stubGlobal("React", React);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("review queue bulk actions", () => {
  it("refreshes the filter that is current when the bulk action completes", async () => {
    const bulkResponse = deferred<{ items: BulkReviewActionItemResult[] }>();
    bffFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === "POST") return bulkResponse.promise;
      throw new Error(`Unexpected path: ${path}`);
    });
    bffFetchEnvelope.mockImplementation(
      (path: string, options?: { signal?: AbortSignal }) => {
        queueRequests.push({ path, signal: options?.signal });
        const rows = queryParam(path, "queue_state") === "expired" ? EXPIRED_ROWS : OPEN_ROWS;
        return Promise.resolve(queueResponse(rows));
      },
    );
    const user = userEvent.setup();

    render(<ReviewPage />);
    await screen.findByText("Alpha Person");

    await startBulkAction(user, "case-a");

    // The bulk request is still in flight on the "Open" filter: switch to the
    // "Expired" filter and let that page load first.
    await user.click(stateFilterPill());
    await user.click(screen.getByRole("button", { name: "Expired" }));
    await screen.findByText("Beta Person");

    bulkResponse.resolve({ items: [bulkOutcome("case-a")] });

    await waitFor(() => expect(screen.getByText("1 of 1 review case(s) updated.")).toBeTruthy());

    // The refresh must target the filter that is on screen, not the one the
    // bulk action was started on.
    const lastRequest = queueRequests[queueRequests.length - 1];
    expect(lastRequest).toBeDefined();
    expect(queryParam(lastRequest?.path ?? "", "queue_state")).toBe("expired");
    // The "Open" rows must not replace the "Expired" rows.
    expect(screen.getByText("Beta Person")).toBeTruthy();
    expect(screen.queryByText("Alpha Person")).toBeNull();
  });

  it("aborts a pending queue reload when the page unmounts", async () => {
    const bulkResponse = deferred<{ items: BulkReviewActionItemResult[] }>();
    const pendingReload = deferred<QueueResponse>();
    bffFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === "POST") return bulkResponse.promise;
      throw new Error(`Unexpected path: ${path}`);
    });
    bffFetchEnvelope.mockImplementation(
      (path: string, options?: { signal?: AbortSignal }) => {
        queueRequests.push({ path, signal: options?.signal });
        // Only the first page load resolves; the reload after the bulk action
        // stays pending.
        if (queueRequests.length === 1) return Promise.resolve(queueResponse(OPEN_ROWS));
        return pendingReload.promise;
      },
    );
    const user = userEvent.setup();

    const view = render(<ReviewPage />);
    await screen.findByText("Alpha Person");

    await startBulkAction(user, "case-a");
    bulkResponse.resolve({ items: [bulkOutcome("case-a")] });

    await waitFor(() => expect(queueRequests.length).toBeGreaterThan(1));
    const reloadRequest = queueRequests[queueRequests.length - 1];
    expect(reloadRequest?.signal?.aborted).toBe(false);

    view.unmount();

    await waitFor(() => expect(reloadRequest?.signal?.aborted).toBe(true));
  });

  it("disables the selection checkboxes while a bulk action is in flight", async () => {
    const bulkResponse = deferred<{ items: BulkReviewActionItemResult[] }>();
    bffFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === "POST") return bulkResponse.promise;
      throw new Error(`Unexpected path: ${path}`);
    });
    bffFetchEnvelope.mockImplementation((path: string, options?: { signal?: AbortSignal }) => {
      queueRequests.push({ path, signal: options?.signal });
      return Promise.resolve(queueResponse(OPEN_ROWS));
    });
    const user = userEvent.setup();

    render(<ReviewPage />);
    await screen.findByText("Alpha Person");
    expect(rowCheckbox("case-a").disabled).toBe(false);

    await startBulkAction(user, "case-a");

    await waitFor(() => expect(rowCheckbox("case-a").disabled).toBe(true));
    expect(selectAllCheckbox().disabled).toBe(true);

    bulkResponse.resolve({ items: [bulkOutcome("case-a")] });

    await waitFor(() => expect(screen.getByText("1 of 1 review case(s) updated.")).toBeTruthy());
    expect(rowCheckbox("case-a").disabled).toBe(false);
    expect(selectAllCheckbox().disabled).toBe(false);
  });

  it("does not select stale rows while a new page is loading", async () => {
    const pendingPage = deferred<QueueResponse>();
    bffFetchEnvelope.mockImplementation((path: string, options?: { signal?: AbortSignal }) => {
      queueRequests.push({ path, signal: options?.signal });
      return queryParam(path, "queue_state") === "assigned"
        ? pendingPage.promise
        : Promise.resolve(queueResponse(OPEN_ROWS));
    });
    const user = userEvent.setup();

    render(<ReviewPage />);
    await screen.findByText("Alpha Person");
    await user.click(stateFilterPill());
    const popover = stateFilterPill().nextElementSibling;
    if (!(popover instanceof HTMLElement)) throw new Error("State filter popover not found");
    await user.click(within(popover).getByRole("button", { name: "Assigned" }));

    await waitFor(() => expect(selectAllCheckbox().disabled).toBe(true));
    expect(screen.queryByLabelText("Select review case case-a")).toBeNull();
    pendingPage.resolve(queueResponse([]));
    await screen.findByText("No review cases found.");
  });
});
