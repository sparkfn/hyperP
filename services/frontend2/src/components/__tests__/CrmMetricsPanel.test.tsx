// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  PersonCrmActivityMetrics,
  PersonCrmDealMetrics,
} from "@/lib/api-types";
import CrmMetricsPanel from "../CrmMetricsPanel";

const deals: PersonCrmDealMetrics = {
  deal_count: 2,
  deal_stage_breakdown: [{ stage_id: "won", count: 2 }],
  first_deal_at: null,
  first_deal_at_display: null,
  last_deal_at: null,
  last_deal_at_display: null,
  conversation_count: 1,
  last_conversation_at: null,
  last_conversation_at_display: null,
  recent_30d_deal_count: 1,
  recent_30d_conversation_count: 1,
  recent_30d_daily_deal_counts: Array(30).fill(0),
  recent_30d_daily_conversation_counts: Array(30).fill(0),
  recent_30d_deal_change_pct: null,
  recent_30d_conversation_change_pct: null,
  last_graph_crm_touch_at: null,
  last_graph_crm_touch_at_display: null,
  days_since_last_deal: null,
  entity_breakdown: [],
};

const shared = {
  source: "bitrix_crm_activity" as const,
  source_instance: "bitrix-primary",
  fetched_at: "2026-09-05T00:00:00Z",
  fetched_at_display: "05 Sep 2026",
  cache_disposition: "miss" as const,
  queried_deal_count: 2,
  resolved_deal_count: 2,
  request_count: 1,
  page_count: 1,
  row_count: 0,
};

const unavailable: PersonCrmActivityMetrics = {
  ...shared,
  status: "unavailable",
  completeness: "unavailable",
  truncated: false,
  failure_reason: "timeout",
};

function aggregate(status: "complete" | "partial"): PersonCrmActivityMetrics {
  const fields = {
    ...shared,
    activity_count: status === "complete" ? 0 : 3,
    call_count: status === "complete" ? 0 : 1,
    activity_kind_breakdown: status === "complete" ? [] : [
      { history_kind: "call", count: 1, last_event_at: null, last_event_at_display: null },
    ],
    call_classification_breakdown: [],
    first_activity_at: null,
    first_activity_at_display: null,
    last_activity_at: null,
    last_activity_at_display: null,
    recent_30d_activity_count: status === "complete" ? 0 : 2,
    recent_30d_call_count: status === "complete" ? 0 : 1,
    recent_30d_daily_activity_counts: Array(30).fill(0),
    recent_30d_daily_call_counts: Array(30).fill(0),
    recent_30d_activity_change_pct: null,
    recent_30d_call_change_pct: null,
  };
  return status === "complete"
    ? { ...fields, status, completeness: status, truncated: false, failure_reason: null }
    : {
        ...fields,
        status,
        completeness: status,
        truncated: true,
        failure_reason: "page_limit",
      };
}

function response(data: object): Response {
  return new Response(JSON.stringify({
    data,
    meta: { request_id: "request-1", next_cursor: null, total_count: null },
  }));
}

function renderPanel(onTotalLoaded = vi.fn()): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CrmMetricsPanel personId="person/one" onTotalLoaded={onTotalLoaded} />
    </QueryClientProvider>,
  );
}

function splitFetch(activity: PersonCrmActivityMetrics): ReturnType<typeof vi.fn> {
  return vi.fn((url: string, init?: RequestInit) => {
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    return Promise.resolve(response(url.includes("deal-metrics") ? deals : activity));
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CrmMetricsPanel split reads", () => {
  it("keeps graph metrics visible while live activity remains pending", async () => {
    const fetchMock = vi.fn((url: string) => url.includes("deal-metrics")
      ? Promise.resolve(response(deals))
      : new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();

    expect(await screen.findByText("2 all time")).toBeTruthy();
    expect(screen.getByText(/Live activity loading/)).toBeTruthy();
    expect(screen.getAllByText("Unavailable")).toHaveLength(3);
    expect(screen.queryByLabelText(/^Activities:/)).toBeNull();
  });

  it("does not turn an unavailable upstream read into zero", async () => {
    vi.stubGlobal("fetch", splitFetch(unavailable));

    renderPanel();

    expect(await screen.findByText(/Live activity unavailable: timeout/)).toBeTruthy();
    expect(screen.getAllByText("Unavailable")).toHaveLength(3);
    expect(screen.queryByText("0 Activities")).toBeNull();
    expect(screen.queryByLabelText(/^Calls:/)).toBeNull();
  });

  it("renders partial aggregates as lower bounds and omits incomplete series", async () => {
    const onTotalLoaded = vi.fn();
    vi.stubGlobal("fetch", splitFetch(aggregate("partial")));

    renderPanel(onTotalLoaded);

    expect(await screen.findByText("≥3 all time")).toBeTruthy();
    expect(screen.getByText("≥1 all time")).toBeTruthy();
    expect(screen.getByText("≥3 total")).toBeTruthy();
    expect(screen.queryByLabelText(/^Activities:/)).toBeNull();
    await waitFor(() => expect(onTotalLoaded).toHaveBeenLastCalledWith(3));
  });

  it("renders confirmed complete zero and includes only complete live totals in the badge", async () => {
    const onTotalLoaded = vi.fn();
    vi.stubGlobal("fetch", splitFetch(aggregate("complete")));

    renderPanel(onTotalLoaded);

    expect(await screen.findByText("No activities on record.")).toBeTruthy();
    expect(screen.getAllByText("0 all time")).toHaveLength(2);
    expect(screen.getAllByLabelText(/^Activities:/).length).toBeGreaterThan(0);
    await waitFor(() => expect(onTotalLoaded).toHaveBeenLastCalledWith(3));
  });

  it("retries only unavailable live activity while preserving graph content", async () => {
    let dealFetches = 0;
    let activityFetches = 0;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      if (url.includes("deal-metrics")) {
        dealFetches += 1;
        return Promise.resolve(response(deals));
      }
      activityFetches += 1;
      return activityFetches === 1
        ? Promise.reject(new TypeError("network failed"))
        : Promise.resolve(response(unavailable));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();

    const retry = await screen.findByRole("button", { name: "Retry live activity metrics" });
    expect(screen.getByText("2 all time")).toBeTruthy();
    fireEvent.click(retry);

    await waitFor(() => expect(activityFetches).toBe(2));
    expect(dealFetches).toBe(1);
    expect(screen.getByText("2 all time")).toBeTruthy();
  });

  it("uses chronological timestamps when graph touch is newer than complete live data", async () => {
    const graphNewer: PersonCrmDealMetrics = {
      ...deals,
      last_graph_crm_touch_at: "2026-09-05T00:00:00-07:00",
      last_graph_crm_touch_at_display: "Graph newest",
    };
    const complete = aggregate("complete");
    if (complete.status !== "complete") throw new Error("complete fixture expected");
    const liveOlder: PersonCrmActivityMetrics = {
      ...complete,
      last_activity_at: "2026-09-05T06:00:00+00:00",
      last_activity_at_display: "Live older",
    };
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(
      response(url.includes("deal-metrics") ? graphNewer : liveOlder),
    )));

    renderPanel();

    const lastCrm = await screen.findByText("Graph newest");
    expect(lastCrm.parentElement?.textContent).toContain("Last CRM: Graph newest");
    expect(screen.getByText("Live older")).toBeTruthy();
  });
});
