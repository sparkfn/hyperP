// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React, { type ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ProfileAnalysisPanel from "../ProfileAnalysisPanel";

const POLL_INTERVAL_MS = 5_000;

const currentAnalysis = {
  analysis_id: "analysis-sales-1",
  person_id: "person/one",
  analysis_type: "sales",
  status: "succeeded",
  content: "Retained sales guidance.\n\nLimitations: Evidence is sparse.",
  input_revision: 7,
  input_fingerprint: "fingerprint",
  prompt_version: "sales-profile-v1",
  provider: "proclaude",
  model: "analysis-model",
  started_at: "2026-07-21T01:00:00+00:00",
  completed_at: "2026-07-21T01:02:00+00:00",
  completed_at_display: "21 Jul 2026, 09:02 AM",
  generated_age_display: "6 days ago",
  valid_until: "2026-07-28T01:02:00+00:00",
  valid_until_display: "28 Jul 2026, 09:02 AM",
  attempt_number: 1,
} as const;

function analysisResponse(
  refreshState: "disabled" | "failed" | "pending" | "running" | "retrying" | "ready",
  retryAttemptsRemaining = 3,
): Response {
  const active = ["pending", "running", "retrying"].includes(refreshState);
  const failed = refreshState === "failed";
  const salesCurrent = active ? currentAnalysis : failed
    ? null
    : { ...currentAnalysis, input_revision: 8 };
  const contactCurrent = active || failed ? null : {
    ...currentAnalysis,
    analysis_id: "analysis-contact-1",
    analysis_type: "contact_tracing",
    input_revision: 8,
  };
  return new Response(JSON.stringify({
    data: {
      input_revision: 8,
      refresh_state: refreshState,
      sales: {
        current: salesCurrent,
        stale: active,
        expired: false,
        valid: !active,
        invalid_reason: active ? "stale" : null,
        refresh_state: refreshState,
        failure_code: failed ? "provider_unavailable" : null,
        auto_request_allowed: false,
        next_retry_at: null,
        next_retry_at_display: null,
        retry_allowed: failed && retryAttemptsRemaining > 0,
        retry_attempts_remaining: retryAttemptsRemaining,
        retry_available_at: retryAttemptsRemaining === 0
          ? "2026-07-28T02:00:00+00:00"
          : null,
        retry_available_at_display: retryAttemptsRemaining === 0
          ? "28 Jul 2026, 10:00 AM"
          : null,
        force_attempts_remaining: 3,
        force_available_at: null,
        force_available_at_display: null,
      },
      contact_tracing: {
        current: contactCurrent,
        stale: false,
        expired: false,
        valid: !active && contactCurrent !== null,
        invalid_reason: active ? "missing" : null,
        refresh_state: refreshState === "ready" ? "ready" : refreshState,
        failure_code: failed ? "provider_unavailable" : null,
        auto_request_allowed: false,
        next_retry_at: null,
        next_retry_at_display: null,
        retry_allowed: failed && retryAttemptsRemaining > 0,
        retry_attempts_remaining: retryAttemptsRemaining,
        retry_available_at: retryAttemptsRemaining === 0
          ? "2026-07-28T02:00:00+00:00"
          : null,
        retry_available_at_display: retryAttemptsRemaining === 0
          ? "28 Jul 2026, 10:00 AM"
          : null,
        force_attempts_remaining: 3,
        force_available_at: null,
        force_available_at_display: null,
      },
    },
    meta: { request_id: "request-1", next_cursor: null },
  }), { status: 200 });
}

function renderPanel(): ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <ProfileAnalysisPanel personId="person/one" />
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ProfileAnalysisPanel", () => {
  it("renders a loading state while its isolated request is pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));

    render(renderPanel());

    expect(screen.getByRole("status").textContent).toContain("Loading profile analysis");
  });

  it("renders a safe error when the response is malformed", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      data: { refresh_state: "not-valid" },
      meta: { request_id: "request-1", next_cursor: null },
    }), { status: 200 })));

    render(renderPanel());

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Profile analysis could not be loaded.",
    );
  });

  it("renders an upstream error without disturbing the containing page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: { code: "upstream_error", message: "Analysis service unavailable." },
      meta: { request_id: "request-1", next_cursor: null },
    }), { status: 503 })));

    render(renderPanel());

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Analysis service unavailable.",
    );
  });

  it("polls pending and running analyses, then stops when ready", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(analysisResponse("pending"))
      .mockResolvedValueOnce(analysisResponse("running"))
      .mockResolvedValueOnce(analysisResponse("ready"));
    vi.stubGlobal("fetch", fetchMock);

    render(renderPanel());
    expect(await screen.findByText("Refresh queued")).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findByText("Refreshing")).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
    expect(await screen.findAllByText("Up to date")).toHaveLength(2);

    await act(async () => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("shows disabled generation without polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn().mockResolvedValue(analysisResponse("disabled"));
    vi.stubGlobal("fetch", fetchMock);

    render(renderPanel());
    expect(await screen.findAllByText("Generation paused")).toHaveLength(3);

    await act(async () => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retains cached content and warns when a background refresh fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(analysisResponse("pending"))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        error: { code: "upstream_error", message: "Analysis refresh unavailable." },
        meta: { request_id: "request-2", next_cursor: null },
      }), { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);

    render(renderPanel());
    expect(await screen.findByText(/Retained sales guidance/)).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));

    const warning = await screen.findByRole("status", { name: "Profile analysis refresh warning" });
    expect(warning.textContent).toContain("Analysis refresh unavailable.");
    expect(warning.getAttribute("aria-live")).toBe("polite");
    expect(screen.getByText(/Retained sales guidance/)).toBeTruthy();
  });

  it("submits an explicit failed-analysis retry and refreshes its state", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(analysisResponse("failed"))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: {
          request_id: "retry-request-1",
          person_id: "person/one",
          analysis_type: "sales",
          state: "queued",
          retry_attempts_remaining: 2,
          retry_available_at: null,
          retry_available_at_display: null,
        },
        meta: { request_id: "request-2", next_cursor: null },
      }), { status: 202 }))
      .mockResolvedValue(analysisResponse("pending"));
    vi.stubGlobal("fetch", fetchMock);

    render(renderPanel());
    const retryButtons = await screen.findAllByRole("button", { name: "Retry analysis" });
    fireEvent.click(retryButtons[0] as HTMLElement);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const retryCall = fetchMock.mock.calls[1];
    expect(retryCall?.[0]).toContain("/bff/persons/person%2Fone/profile-analyses/retries");
    expect(retryCall?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ analysis_type: "sales" }),
    });
    expect(await screen.findByText("Refresh queued")).toBeTruthy();
  });

  it("refreshes the shared retry budget after a rate-limit response", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(analysisResponse("failed"))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        error: {
          code: "profile_analysis_retry_limit",
          message: "The profile analysis retry limit has been reached. Try again later.",
          details: {
            retry_available_at: "2026-07-28T02:00:00+00:00",
            retry_available_at_display: "28 Jul 2026, 10:00 AM",
          },
        },
        meta: { request_id: "request-2", next_cursor: null },
      }), { status: 429 }))
      .mockResolvedValue(analysisResponse("failed", 0));
    vi.stubGlobal("fetch", fetchMock);

    render(renderPanel());
    const retryButtons = await screen.findAllByRole("button", { name: "Retry analysis" });
    fireEvent.click(retryButtons[0] as HTMLElement);

    expect((await screen.findByRole("alert")).textContent).toContain(
      "The retry limit has been reached until 28 Jul 2026, 10:00 AM.",
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const refreshedButtons = await screen.findAllByRole("button", { name: "Retry analysis" });
    expect(refreshedButtons.every((button) => button.hasAttribute("disabled"))).toBe(true);
  });
});
