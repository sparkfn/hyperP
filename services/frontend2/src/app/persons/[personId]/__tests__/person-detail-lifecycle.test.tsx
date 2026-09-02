// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React, { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { bffFetch, bffFetchEnvelope } = vi.hoisted(() => ({
  bffFetch: vi.fn(),
  bffFetchEnvelope: vi.fn(),
}));

vi.mock("@/lib/api-client", () => ({
  BffError: class BffError extends Error {},
  bffFetch,
  bffFetchEnvelope,
}));
vi.mock("@/lib/usePaginatedFetch", () => ({
  usePaginatedFetch: () => ({ rows: [] }),
}));
vi.mock("next/link", () => ({ default: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("next/navigation", () => ({ notFound: vi.fn(), useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/components/ActionToast", () => ({ default: () => null }));
vi.mock("@/components/CrmMetricsPanel", () => ({ default: () => null }));
vi.mock("@/components/MergeOverlay", () => ({ default: () => null }));
vi.mock("@/components/PersonGraphDialog", () => ({ default: () => null }));
vi.mock("@/components/ProfileAnalysisPanel", () => ({ default: () => null }));
vi.mock("@/components/ReviewActionsPanel", () => ({ default: () => null }));
vi.mock("@/app/review/[reviewCaseId]/ReviewCaseDetailModal", () => ({ ReviewCaseDetailModal: () => null }));

import type { Person } from "@/lib/api-types";
import type { PersonAuditEvent, PersonIdentifier } from "@/lib/api-types-person";
import type { ReviewCaseSummary } from "@/lib/api-types-ops";
import { MatchesTab, TimelineTab, type DetailData } from "../page";

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (error: Error) => void } {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => { resolve = resolvePromise; reject = rejectPromise; });
  return { promise, resolve, reject };
}

function person(personId = "person-a"): Person {
  return {
    person_id: personId, status: "active", is_high_value: false, is_high_risk: false,
    preferred_full_name: "Person A", preferred_phone: null, preferred_email: null,
    preferred_dob: null, preferred_address: null, preferred_nric: null,
    preferred_race_ethnicity: null, profile_completeness_score: 0.5,
    golden_profile_computed_at: null, golden_profile_version: null, source_record_count: 0,
    connection_count: 0, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
  };
}

function reviewCase(currentPersonId = "person-a"): ReviewCaseSummary {
  return {
    review_case_id: "case-1", queue_state: "open", priority: 1, assigned_to: null,
    follow_up_at: null, sla_due_at: null, resolution: null, resolved_at: null,
    left_person_id: currentPersonId, left_person_name: "Current person", left_person_status: "active",
    right_person_id: "person-b", right_person_name: "Person B", right_person_status: "active",
    match_decision: { match_decision_id: "decision-1", engine_type: "heuristic", decision: "review", confidence: 0.9 },
  };
}

function sharedPhoneDetail(value: string): unknown {
  return {
    candidate_person_id: "person-b",
    shared_identifier_groups: [{
      identifier_type: "phone",
      normalized_value: value,
      current_person_source_records: [],
      candidate_source_records: [],
    }],
  };
}

const emptyDetail: DetailData = {
  identifiers: [], sourceRecords: [], sales: [], audit: [], bankruptcyCases: [], sourceRecordFacets: [],
};

function matchesTab(personId = "person-a"): React.ReactElement {
  return (
    <MatchesTab
      personId={personId}
      currentPerson={person(personId)}
      currentIdentifiers={[] as PersonIdentifier[]}
      activeMatchesTab="candidates"
      onTotalLoaded={() => undefined}
      onMergeWith={() => undefined}
    />
  );
}

function renderMatches(personId = "person-a"): ReturnType<typeof render> {
  return render(matchesTab(personId));
}

function auditEvent(): PersonAuditEvent {
  return {
    merge_event_id: "audit-1", event_type: "manual_merge", actor_type: "user", actor_id: "reviewer",
    reason: null, metadata: {}, created_at: "2026-01-02T00:00:00Z", absorbed_person_id: null,
    survivor_person_id: null, triggered_by_decision_id: null,
  };
}

beforeEach(() => {
  bffFetch.mockReset();
  bffFetchEnvelope.mockReset();
  class ImmediateIntersectionObserver {
    constructor(callback: IntersectionObserverCallback) { queueMicrotask(() => callback([{ isIntersecting: true } as IntersectionObserverEntry], this)); }
    observe(): void {}
    disconnect(): void {}
    unobserve(): void {}
    takeRecords(): IntersectionObserverEntry[] { return []; }
    root = null;
    rootMargin = "0px";
    thresholds = [];
  }
  vi.stubGlobal("IntersectionObserver", ImmediateIntersectionObserver);
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("person detail deferred lifecycle", () => {
  it("renders the current person's recommended-match error instead of keeping its loading shell", async () => {
    bffFetchEnvelope.mockImplementation((path: string) => {
      if (path.includes("/review-cases?")) return Promise.reject(new Error("offline"));
      return Promise.resolve({ data: [] });
    });
    bffFetch.mockResolvedValue(person("person-b"));

    renderMatches();

    expect(await screen.findByText("Failed to load recommended matches.")).toBeTruthy();
    expect(screen.queryByText("No recommended matches found.")).toBeNull();
  });

  it("renders loading, ignores an aborted stale completion, and replaces a transient failure", async () => {
    const staleDetail = deferred<unknown>();
    const failedDetail = deferred<unknown>();
    const replacementDetail = deferred<unknown>();
    const detailPromises = [staleDetail, failedDetail, replacementDetail];
    const detailSignals: AbortSignal[] = [];
    let detailRequests = 0;
    bffFetchEnvelope.mockImplementation((path: string) => {
      return Promise.resolve({ data: path.includes("/review-cases?") ? [reviewCase()] : [] });
    });
    bffFetch.mockImplementation((path: string, options?: { signal?: AbortSignal }) => {
      if (path.includes("shared-identifiers")) {
        const next = detailPromises[detailRequests++];
        if (next === undefined) throw new Error("unexpected recommended-detail request");
        if (options?.signal !== undefined) detailSignals.push(options.signal);
        return next.promise;
      }
      if (path.includes("/review-cases/")) return new Promise(() => undefined);
      return Promise.resolve(person("person-b"));
    });

    const first = renderMatches();
    await screen.findByText("Person B");
    fireEvent.click(screen.getByLabelText("Toggle match detail"));
    expect(await screen.findByText("Loading match comparison…")).toBeTruthy();
    await waitFor(() => expect(detailSignals).toHaveLength(1));

    first.rerender(matchesTab("person-c"));
    expect(detailSignals[0]?.aborted).toBe(true);
    expect(detailSignals).toHaveLength(1);
    await waitFor(() => expect(detailSignals).toHaveLength(2));
    fireEvent.click(screen.getByLabelText("Toggle match detail"));
    expect(await screen.findByText("Loading match comparison…")).toBeTruthy();

    await act(async () => { staleDetail.resolve(sharedPhoneDetail("stale-phone")); });
    expect(screen.queryByText("stale-phone")).toBeNull();
    await act(async () => { failedDetail.reject(new Error("temporary failure")); });
    expect(await screen.findByText("temporary failure")).toBeTruthy();

    first.rerender(matchesTab("person-d"));
    expect(detailSignals[1]?.aborted).toBe(true);
    expect(detailSignals).toHaveLength(2);
    await waitFor(() => expect(detailSignals).toHaveLength(3));
    fireEvent.click(screen.getByLabelText("Toggle match detail"));
    expect(await screen.findByText("Loading match comparison…")).toBeTruthy();
    await act(async () => { replacementDetail.resolve(sharedPhoneDetail("fresh-phone")); });
    expect((await screen.findAllByText("fresh-phone")).length).toBe(2);
    await waitFor(() => expect(screen.queryByText("temporary failure")).toBeNull());
  });

  it("coalesces expansion with automatic review-detail preload and aborts it on unmount", async () => {
    const reviewDetail = deferred<unknown>();
    bffFetchEnvelope.mockImplementation((path: string) => Promise.resolve({ data: path.includes("/review-cases?") ? [reviewCase()] : [] }));
    bffFetch.mockImplementation((path: string) => {
      if (path.includes("/review-cases/")) return reviewDetail.promise;
      if (path.includes("shared-identifiers")) return new Promise(() => undefined);
      return Promise.resolve(person("person-b"));
    });

    const view = renderMatches();
    await screen.findByText("Person B");
    await waitFor(() => expect(bffFetch.mock.calls.filter(([path]) => String(path).includes("/review-cases/case-1"))).toHaveLength(1));
    fireEvent.click(screen.getByLabelText("Toggle match detail"));
    expect(bffFetch.mock.calls.filter(([path]) => String(path).includes("/review-cases/case-1"))).toHaveLength(1);
    const signal = bffFetch.mock.calls.find(([path]) => String(path).includes("/review-cases/case-1"))?.[1]?.signal;
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("keeps an all-failed timeline incomplete and retains a successful audit through a failed retry", async () => {
    let attempt = 0;
    bffFetchEnvelope.mockImplementation((path: string) => {
      const currentAttempt = attempt;
      if (path.includes("/audit")) {
        return currentAttempt === 1
          ? Promise.resolve({ data: [auditEvent()] })
          : Promise.reject(new Error("offline"));
      }
      return Promise.reject(new Error("offline"));
    });

    render(<TimelineTab person={person()} detailData={emptyDetail} personId="person-a" onActivityCountLoaded={() => undefined} />);
    expect(await screen.findByText(/Timeline incomplete/)).toBeTruthy();
    expect(screen.queryByText("No events recorded.")).toBeNull();

    attempt = 1;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("manual_merge")).toBeTruthy();
    expect(await screen.findByText(/Timeline incomplete/)).toBeTruthy();

    attempt = 2;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("manual_merge")).toBeTruthy();
    expect(screen.queryByText("No events recorded.")).toBeNull();
    expect(await screen.findByText(/Timeline incomplete/)).toBeTruthy();
  });
});
