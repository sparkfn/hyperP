// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  PersonProfileAnalyses,
  ProfileAnalysisCurrent,
  ProfileAnalysisSlot,
} from "@/lib/api-types-person";
import ProfileAnalysisCards from "../ProfileAnalysisCards";

afterEach(cleanup);

const salesCurrent: ProfileAnalysisCurrent = {
  analysis_id: "analysis-sales-1",
  person_id: "person-1",
  analysis_type: "sales",
  status: "succeeded",
  content: "Repeat purchases are supported by [order-1].\n\nLimitations: History is sparse.",
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
};

function slot(overrides: Partial<ProfileAnalysisSlot> = {}): ProfileAnalysisSlot {
  return {
    current: null,
    stale: false,
    expired: false,
    valid: false,
    invalid_reason: "missing",
    refresh_state: "pending",
    failure_code: null,
    auto_request_allowed: false,
    next_retry_at: null,
    next_retry_at_display: null,
    retry_allowed: false,
    retry_attempts_remaining: 3,
    retry_available_at: null,
    retry_available_at_display: null,
    force_attempts_remaining: 3,
    force_available_at: null,
    force_available_at_display: null,
    ...overrides,
  };
}

function analyses(overrides: Partial<PersonProfileAnalyses> = {}): PersonProfileAnalyses {
  return {
    input_revision: 7,
    refresh_state: "ready",
    sales: slot({ current: salesCurrent, refresh_state: "ready" }),
    contact_tracing: slot({ refresh_state: "ready" }),
    ...overrides,
  };
}

function card(name: string): HTMLElement {
  return screen.getByRole("heading", { name }).closest("article") as HTMLElement;
}

describe("ProfileAnalysisCards", () => {
  it("renders two independent ready cards with API display metadata", () => {
    const contact = {
      ...salesCurrent,
      analysis_id: "analysis-contact-1",
      analysis_type: "contact_tracing" as const,
    };
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      contact_tracing: slot({ current: contact, refresh_state: "ready" }),
    })} />);

    expect(screen.getByRole("heading", { name: "Sales" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Contact tracing" })).toBeTruthy();
    expect(screen.getAllByText("Up to date")).toHaveLength(2);
    expect(within(card("Sales")).getByText("Generated 6 days ago")).toBeTruthy();
    expect(within(card("Sales")).getByText("Model analysis-model")).toBeTruthy();
    expect(within(card("Sales")).getByText(/Limitations: History is sparse/)).toBeTruthy();
    const status = screen.getByText("Profile analysis status:").closest("p");
    expect(status?.getAttribute("aria-live")).toBe("polite");
    expect(within(card("Sales")).getByText(/Limitations/).closest("[aria-live]")).toBeNull();
  });

  it("shows partial availability and an independent failed slot", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      refresh_state: "partial",
      contact_tracing: slot({ refresh_state: "failed", failure_code: "provider_unavailable" }),
    })} />);

    expect(screen.getByText("Partially available")).toBeTruthy();
    expect(within(card("Contact tracing")).getByText("Generation failed")).toBeTruthy();
    expect(
      within(card("Contact tracing")).getByText("Latest refresh failed: provider_unavailable"),
    ).toBeTruthy();
  });

  it("retains prior content while a refresh is pending", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      refresh_state: "pending",
      sales: slot({ current: salesCurrent, stale: true, refresh_state: "pending" }),
    })} />);

    expect(within(card("Sales")).getByText("Refresh queued")).toBeTruthy();
    expect(within(card("Sales")).getByText(/Repeat purchases/)).toBeTruthy();
  });

  it("shows an explicit running state when current output is missing", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      refresh_state: "running",
      sales: slot({ refresh_state: "running" }),
    })} />);

    expect(within(card("Sales")).getByText("Generating")).toBeTruthy();
    expect(within(card("Sales")).getByText("Analysis is being generated.")).toBeTruthy();
  });

  it("distinguishes a scheduled retry from terminal failure", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      refresh_state: "retrying",
      sales: slot({ refresh_state: "retrying" }),
    })} />);

    expect(within(card("Sales")).getByText("Retry scheduled")).toBeTruthy();
    expect(within(card("Sales")).getByText("Analysis will retry automatically.")).toBeTruthy();
  });

  it("retains prior content after a failed refresh", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      refresh_state: "failed",
      sales: slot({
        current: salesCurrent,
        stale: true,
        refresh_state: "failed",
        failure_code: "rate_limited",
      }),
    })} />);

    expect(within(card("Sales")).getByText("Refresh failed")).toBeTruthy();
    expect(within(card("Sales")).getByText("Stale")).toBeTruthy();
    expect(within(card("Sales")).getByText(/Repeat purchases/)).toBeTruthy();
  });

  it("offers a bounded retry action for a terminal failure", () => {
    const onRetryRequest = vi.fn();
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={onRetryRequest} analyses={analyses({
      refresh_state: "failed",
      sales: slot({
        refresh_state: "failed",
        failure_code: "provider_unavailable",
        retry_allowed: true,
        retry_attempts_remaining: 2,
      }),
    })} />);

    fireEvent.click(within(card("Sales")).getByRole("button", { name: "Retry analysis" }));

    expect(onRetryRequest).toHaveBeenCalledWith("sales");
    expect(within(card("Sales")).getByText("2 retries available this hour for this person")).toBeTruthy();
  });

  it("disables terminal retry after the per-user Person budget is exhausted", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      refresh_state: "failed",
      sales: slot({
        refresh_state: "failed",
        failure_code: "provider_unavailable",
        retry_attempts_remaining: 0,
        retry_available_at: "2026-07-28T02:00:00+00:00",
        retry_available_at_display: "28 Jul 2026, 10:00 AM",
      }),
    })} />);

    const retryButton = within(card("Sales")).getByRole("button", { name: "Retry analysis" });
    expect(retryButton.hasAttribute("disabled")).toBe(true);
    expect(within(card("Sales")).getByText("Retries available again 28 Jul 2026, 10:00 AM")).toBeTruthy();
  });

  it("disables forced refresh after the rolling-hour budget is exhausted", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      sales: slot({
        current: salesCurrent,
        valid: true,
        invalid_reason: null,
        refresh_state: "ready",
        force_attempts_remaining: 0,
        force_available_at: "2026-07-27T02:00:00+00:00",
        force_available_at_display: "27 Jul 2026, 10:00 AM",
      }),
    })} />);

    expect(
      within(card("Sales")).getByRole("button", { name: "Force new analysis" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(within(card("Sales")).getByText(/Forced refreshes available again/)).toBeTruthy();
  });

  it("renders explicit empty and queued states for missing output", () => {
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      sales: slot({ refresh_state: "ready" }),
      contact_tracing: slot({ refresh_state: "pending" }),
    })} />);

    expect(within(card("Sales")).getByText("An analysis will be generated when this profile is opened.")).toBeTruthy();
    expect(within(card("Contact tracing")).getByText("Analysis is queued.")).toBeTruthy();
  });

  it("renders long HTML-looking output literally", () => {
    const literal = `<img src=x onerror=alert(1)> ${"supported detail ".repeat(120)}`;
    render(<ProfileAnalysisCards requestingTypes={new Set()} onForceRequest={() => undefined} onRetryRequest={() => undefined} analyses={analyses({
      sales: slot({ current: { ...salesCurrent, content: literal }, refresh_state: "ready" }),
    })} />);

    const salesCard = card("Sales");
    expect(salesCard.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(salesCard.querySelector("img")).toBeNull();
    expect(salesCard.textContent).toContain("supported detail supported detail");
  });
});
