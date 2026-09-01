// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React, { type ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CrmMetricsPanel from "../CrmMetricsPanel";
import type { PersonCrmMetrics } from "@/lib/api-types";

const metrics: PersonCrmMetrics = {
  deal_count: 6,
  deal_stage_breakdown: [
    { stage_id: "won", count: 4 },
    { stage_id: "new", count: 2 },
  ],
  first_deal_at: "2026-01-02T08:00:00+08:00",
  first_deal_at_display: "02 Jan 2026",
  last_deal_at: "2026-08-14T09:30:00+08:00",
  last_deal_at_display: "14 Aug 2026",
  activity_count: 47,
  call_count: 18,
  conversation_count: 5,
  activity_kind_breakdown: [
    {
      history_kind: "call",
      count: 18,
      last_event_at: "2026-08-14T10:00:00+08:00",
      last_event_at_display: "14 Aug 2026",
    },
  ],
  first_activity_at: "2026-01-05T08:00:00+08:00",
  first_activity_at_display: "05 Jan 2026",
  last_activity_at: "2026-08-14T10:00:00+08:00",
  last_activity_at_display: "14 Aug 2026",
  entity_breakdown: [
    {
      entity_key: "fundbox",
      entity_display_name: "Fundbox",
      deal_count: 6,
      activity_count: 22,
      conversation_count: 3,
    },
  ],
  recent_30d_deal_count: 2,
  recent_30d_activity_count: 8,
  recent_30d_call_count: 3,
  recent_30d_conversation_count: 1,
  last_crm_touch_at: "2026-08-14T10:00:00+08:00",
  last_crm_touch_at_display: "14 Aug 2026, 02:00 AM",
  days_since_last_crm_touch: 5,
  days_since_last_deal: 5,
  days_since_last_activity: 5,
  recent_30d_daily_deal_counts: [0, 1, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  recent_30d_daily_activity_counts: [1, 0, 1, 1, 0, 2, 0, 1, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  recent_30d_daily_call_counts: [0, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  recent_30d_daily_conversation_counts: [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  recent_30d_deal_change_pct: -50,
  recent_30d_activity_change_pct: -20,
  recent_30d_call_change_pct: 50,
  recent_30d_conversation_change_pct: null,
};

function metricsResponse(data: PersonCrmMetrics): Response {
  return new Response(
    JSON.stringify({
      data,
      meta: { request_id: "request-1", next_cursor: null },
    }),
    { status: 200 },
  );
}

function renderPanel(onTotalLoaded: (total: number) => void): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <CrmMetricsPanel personId="person/one" onTotalLoaded={onTotalLoaded} />
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CrmMetricsPanel", () => {
  it("renders all CRM metric groups", async () => {
    const onTotalLoaded = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => metricsResponse(metrics)));

    render(renderPanel(onTotalLoaded));

    expect(await screen.findByText("won")).toBeTruthy();
    expect(screen.getAllByText("Deals")).toHaveLength(3);
    expect(screen.getAllByText("Activities")).toHaveLength(3);
    expect(screen.getAllByText("Calls")).toHaveLength(2);
    expect(screen.getAllByText("Chats")).toHaveLength(2);
    expect(screen.getByText("won")).toBeTruthy();
    expect(screen.getByText("new")).toBeTruthy();
    expect(screen.getByText("Call")).toBeTruthy();
    expect(screen.getByText("Fundbox")).toBeTruthy();
    expect(screen.getByText("Fundbox").className).toContain("entityName");
    expect(
      screen.getByText("6 deals - 22 Activities - 3 Chats").className,
    ).toContain("entitySummary");
    expect(screen.getByRole("region", { name: "CRM overview" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Recency" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Breakdowns" })).toBeTruthy();
    expect(screen.getAllByText("5 days ago")).toHaveLength(3);
    await waitFor(() => expect(onTotalLoaded).toHaveBeenCalledWith(76));
  });

  it("requests the CRM metrics through the BFF", async () => {
    const fetchMock = vi.fn(async () => metricsResponse(metrics));
    vi.stubGlobal("fetch", fetchMock);

    render(renderPanel(vi.fn()));
    await screen.findByText("won");

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/bff/persons/person%2Fone/crm/metrics"),
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("renders a loading state while the isolated request is pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));

    render(renderPanel(vi.fn()));

    expect(screen.getByRole("status").textContent).toContain(
      "Loading CRM engagement",
    );
  });

  it("renders an empty state for a person without CRM records", async () => {
    const onTotalLoaded = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => metricsResponse({
      ...metrics,
      deal_count: 0,
      deal_stage_breakdown: [],
      first_deal_at: null,
      first_deal_at_display: null,
      last_deal_at: null,
      last_deal_at_display: null,
      activity_count: 0,
      call_count: 0,
      conversation_count: 0,
      activity_kind_breakdown: [],
      first_activity_at: null,
      first_activity_at_display: null,
      last_activity_at: null,
      last_activity_at_display: null,
      entity_breakdown: [],
      recent_30d_deal_count: 0,
      recent_30d_activity_count: 0,
      recent_30d_call_count: 0,
      recent_30d_conversation_count: 0,
      last_crm_touch_at: null,
      last_crm_touch_at_display: null,
      days_since_last_crm_touch: null,
      days_since_last_deal: null,
      days_since_last_activity: null,
      recent_30d_daily_deal_counts: Array(30).fill(0),
      recent_30d_daily_activity_counts: Array(30).fill(0),
      recent_30d_daily_call_counts: Array(30).fill(0),
      recent_30d_daily_conversation_counts: Array(30).fill(0),
      recent_30d_deal_change_pct: null,
      recent_30d_activity_change_pct: null,
      recent_30d_call_change_pct: null,
      recent_30d_conversation_change_pct: null,
    })));

    render(renderPanel(onTotalLoaded));

    expect(await screen.findByText("No CRM records on file.")).toBeTruthy();
    await waitFor(() => expect(onTotalLoaded).toHaveBeenCalledWith(0));
  });

  it("renders a not-found result as an empty state", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: { code: "person_not_found", message: "Person not found." },
      meta: { request_id: "request-1", next_cursor: null },
    }), { status: 404 })));

    render(renderPanel(vi.fn()));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "No CRM records on file.",
    );
  });

  it("renders an upstream error without losing the containing page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: { code: "upstream_error", message: "CRM metrics unavailable." },
      meta: { request_id: "request-1", next_cursor: null },
    }), { status: 503 })));

    render(renderPanel(vi.fn()));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "CRM metrics unavailable.",
    );
  });
});
