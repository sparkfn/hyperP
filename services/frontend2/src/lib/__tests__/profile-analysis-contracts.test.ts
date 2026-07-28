import { describe, expect, it } from "vitest";

import {
  parsePersonProfileAnalyses,
  parseProfileAnalysisHistory,
} from "../profile-analysis-contracts";

const currentAnalysis = {
  analysis_id: "analysis-sales-1",
  person_id: "person-1",
  analysis_type: "sales",
  status: "succeeded",
  content: "Observed repeat purchases.\n\nLimitations: Sparse order history.",
  input_revision: 7,
  input_fingerprint: "sha256-fingerprint",
  prompt_version: "sales-profile-v1",
  provider: "proclaude",
  model: "analysis-model",
  started_at: "2026-07-21T01:00:00+00:00",
  completed_at: "2026-07-21T01:02:00+00:00",
  completed_at_display: "21 Jul 2026, 09:02 AM",
  attempt_number: 2,
} as const;

const retryBudget = {
  retry_allowed: false,
  retry_attempts_remaining: 3,
  retry_available_at: null,
  retry_available_at_display: null,
} as const;

describe("profile analysis response contracts", () => {
  it("accepts complete independent current slots", () => {
    const result = parsePersonProfileAnalyses({
      input_revision: 8,
      refresh_state: "running",
      sales: {
        current: currentAnalysis,
        stale: true,
        refresh_state: "running",
        failure_code: null,
        ...retryBudget,
      },
      contact_tracing: {
        current: null,
        stale: false,
        refresh_state: "failed",
        failure_code: "provider_unavailable",
        ...retryBudget,
        retry_allowed: true,
      },
    }, "person-1");

    expect(result.sales.current?.model).toBe("analysis-model");
    expect(result.contact_tracing.failure_code).toBe("provider_unavailable");
  });

  it("rejects missing or inconsistent shared retry-budget metadata", () => {
    const validSlot = {
      current: null,
      stale: false,
      refresh_state: "pending",
      failure_code: null,
      ...retryBudget,
    };
    const missingRetryField = {
      input_revision: 8,
      refresh_state: "pending",
      sales: validSlot,
      contact_tracing: {
        current: null,
        stale: false,
        refresh_state: "pending",
        failure_code: null,
        retry_attempts_remaining: 3,
        retry_available_at: null,
        retry_available_at_display: null,
      },
    };
    const mismatchedBudget = {
      input_revision: 8,
      refresh_state: "pending",
      sales: validSlot,
      contact_tracing: {
        ...validSlot,
        retry_attempts_remaining: 0,
        retry_available_at: "2026-07-28T02:00:00+00:00",
        retry_available_at_display: "28 Jul 2026, 10:00 AM",
      },
    };

    expect(() => parsePersonProfileAnalyses(missingRetryField, "person-1")).toThrow();
    expect(() => parsePersonProfileAnalyses(mismatchedBudget, "person-1")).toThrow();
  });

  it("accepts complete history provenance and failure metadata", () => {
    const result = parseProfileAnalysisHistory([
      {
        ...currentAnalysis,
        status: "failed",
        content: null,
        failure_code: "invalid_output",
        retryable: false,
        next_retry_at: null,
      },
      {
        ...currentAnalysis,
        analysis_id: "analysis-obsolete-1",
        analysis_type: "contact_tracing",
        status: "obsolete",
        failure_code: null,
        retryable: null,
        next_retry_at: null,
      },
    ]);

    expect(result[0]?.retryable).toBe(false);
    expect(result[1]?.status).toBe("obsolete");
  });

  it.each([
    ["unknown overall state", { refresh_state: "queued" }],
    ["negative revision", { input_revision: -1 }],
    ["unknown slot state", { sales: { refresh_state: "partial" } }],
    ["unexpected field", { unexpected: true }],
  ])("rejects %s", (_label, change) => {
    const base = {
      input_revision: 7,
      refresh_state: "partial",
      sales: {
        current: currentAnalysis,
        stale: false,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
      contact_tracing: {
        current: null,
        stale: false,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
    };

    const candidate = "sales" in change
      ? { ...base, sales: { ...base.sales, ...change.sales } }
      : { ...base, ...change };

    expect(() => parsePersonProfileAnalyses(candidate, "person-1")).toThrow();
  });

  it.each([
    ["sales output in the contact slot", {
      contact_tracing: {
        current: currentAnalysis,
        stale: false,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["contact output in the sales slot", {
      sales: {
        current: { ...currentAnalysis, analysis_type: "contact_tracing" },
        stale: false,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["current output for another Person", {
      sales: {
        current: { ...currentAnalysis, person_id: "person-2" },
        stale: false,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["stale output marked ready", {
      sales: {
        current: currentAnalysis,
        stale: true,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
    }],
  ])("rejects semantic contradiction: %s", (_label, change) => {
    const candidate = {
      input_revision: 7,
      refresh_state: "partial",
      sales: {
        current: currentAnalysis,
        stale: false,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
      contact_tracing: {
        current: null,
        stale: false,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
      ...change,
    };

    expect(() => parsePersonProfileAnalyses(candidate, "person-1")).toThrow();
  });

  it("rejects malformed and extra history fields", () => {
    const malformed = {
      ...currentAnalysis,
      attempt_number: 0,
      failure_code: null,
      retryable: null,
      next_retry_at: null,
      provider_body: "secret upstream response",
    };

    expect(() => parseProfileAnalysisHistory([malformed])).toThrow();
  });

  it.each([
    ["blank successful content", { content: "   " }],
    ["timezone-free start", { started_at: "2026-07-21T01:00:00" }],
    ["malformed completion", { completed_at: "not-a-timestamp" }],
    ["completion before start", { completed_at: "2026-07-21T00:59:59+00:00" }],
  ])("rejects unsafe current analysis provenance: %s", (_label, currentChange) => {
    const candidate = {
      input_revision: 8,
      refresh_state: "pending",
      sales: {
        current: { ...currentAnalysis, ...currentChange },
        stale: true,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
      contact_tracing: {
        current: null,
        stale: false,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
    };

    expect(() => parsePersonProfileAnalyses(candidate, "person-1")).toThrow();
  });

  it.each([
    ["stale flag disagrees with revision", {
      refresh_state: "partial",
      sales: {
        current: currentAnalysis,
        stale: false,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["missing current is stale", {
      refresh_state: "pending",
      sales: {
        current: null,
        stale: true,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["ready slot has no fresh current", {
      refresh_state: "partial",
      sales: {
        current: null,
        stale: false,
        refresh_state: "ready",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["failure code appears outside failed state", {
      refresh_state: "pending",
      sales: {
        current: currentAnalysis,
        stale: true,
        refresh_state: "pending",
        failure_code: "provider_unavailable",
        ...retryBudget,
      },
    }],
    ["unsafe failure code crosses the trust boundary", {
      refresh_state: "failed",
      sales: {
        current: currentAnalysis,
        stale: true,
        refresh_state: "failed",
        failure_code: "Provider error: private body",
        ...retryBudget,
        retry_allowed: true,
      },
    }],
    ["disabled state applies to only one slot", {
      refresh_state: "disabled",
      sales: {
        current: currentAnalysis,
        stale: true,
        refresh_state: "disabled",
        failure_code: null,
        ...retryBudget,
      },
    }],
    ["overall state disagrees with slot precedence", {
      refresh_state: "ready",
      sales: {
        current: currentAnalysis,
        stale: true,
        refresh_state: "running",
        failure_code: null,
        ...retryBudget,
      },
    }],
  ])("rejects state contradiction: %s", (_label, change) => {
    const candidate = {
      input_revision: 8,
      contact_tracing: {
        current: null,
        stale: false,
        refresh_state: "pending",
        failure_code: null,
        ...retryBudget,
      },
      ...change,
    };

    expect(() => parsePersonProfileAnalyses(candidate, "person-1")).toThrow();
  });

  it.each([
    ["timezone-free start", { started_at: "2026-07-21T01:00:00" }],
    ["completion before start", { completed_at: "2026-07-21T00:59:59+00:00" }],
    ["blank success", { content: "  " }],
    ["success with failure metadata", { failure_code: "invalid_output", retryable: false }],
    ["failed attempt with content", {
      status: "failed",
      content: "private provider body",
      failure_code: "invalid_output",
      retryable: false,
    }],
    ["failed attempt without retryability", {
      status: "failed",
      content: null,
      failure_code: "invalid_output",
    }],
    ["retryable failure without retry time", {
      status: "failed",
      content: null,
      failure_code: "provider_unavailable",
      retryable: true,
    }],
    ["non-retryable failure with retry time", {
      status: "failed",
      content: null,
      failure_code: "invalid_output",
      retryable: false,
      next_retry_at: "2026-07-21T02:02:00+00:00",
    }],
    ["obsolete attempt with failure metadata", {
      status: "obsolete",
      content: null,
      failure_code: "stale_revision",
      retryable: false,
    }],
    ["unsafe failure code", {
      status: "failed",
      content: null,
      failure_code: "provider/private",
      retryable: false,
    }],
  ])("rejects incoherent history item: %s", (_label, change) => {
    const item = {
      ...currentAnalysis,
      failure_code: null,
      retryable: null,
      next_retry_at: null,
      ...change,
    };

    expect(() => parseProfileAnalysisHistory([item])).toThrow();
  });
});
