// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React, { type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
vi.mock("@/app/review/[reviewCaseId]/ReviewCaseDetailModal", () => ({
  ReviewCaseDetailModal: () => null,
}));

import type { PersonIdentifier, PersonSourceRecord } from "@/lib/api-types-person";
import { IdentifiersTab } from "../page-content";

function sourceRecord(
  overrides: Pick<PersonSourceRecord, "source_record_pk" | "source_system"> &
    Partial<PersonSourceRecord>,
): PersonSourceRecord {
  return {
    entity_key: null,
    entity_display_name: null,
    source_record_id: overrides.source_record_pk,
    source_record_version: null,
    record_type: "identity",
    lifecycle_status: "active",
    extraction_method: null,
    extraction_confidence: null,
    link_status: "linked",
    linked_person_id: null,
    observed_at: "2026-08-20T00:00:00Z",
    ingested_at: "2026-08-20T00:00:00Z",
    normalized_payload: null,
    raw_payload: null,
    conversation_ref: null,
    observed_at_display: "20 Aug 2026",
    ingested_at_display: "20 Aug 2026",
    extraction_confidence_display: null,
    chat_transcript: null,
    ...overrides,
  };
}

function identifier(
  overrides: Pick<PersonIdentifier, "identifier_type" | "normalized_value"> &
    Partial<PersonIdentifier>,
): PersonIdentifier {
  return {
    is_active: true,
    is_verified: false,
    last_confirmed_at: null,
    source_system_key: null,
    source_record_ids: null,
    entities: [],
    source_records: [],
    ...overrides,
  };
}

function renderTab(identifiers: PersonIdentifier[]): void {
  vi.stubGlobal("React", React);
  render(
    <IdentifiersTab
      identifiers={identifiers}
      totalCount={identifiers.length}
      nextCursor={null}
      loadingMore={false}
      onLoadMore={async () => undefined}
    />,
  );
}

function multiSourceIdentifier(): PersonIdentifier {
  return identifier({
    identifier_type: "phone",
    normalized_value: "+6591234567",
    source_system_key: "fundbox",
    source_records: [
      sourceRecord({ source_record_pk: "sr-1", source_system: "fundbox" }),
      sourceRecord({ source_record_pk: "sr-2", source_system: "fundbox" }),
      sourceRecord({ source_record_pk: "sr-3", source_system: "bitrix_chat" }),
    ],
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("IdentifiersTab contributing systems", () => {
  it("shows one collapsed chip per distinct contributing system", () => {
    renderTab([multiSourceIdentifier()]);

    expect(screen.getAllByText("fundbox")).toHaveLength(1);
    expect(screen.getAllByText("bitrix_chat")).toHaveLength(1);
  });

  it("renders every corroborating source record when expanded", async () => {
    const user = userEvent.setup();
    renderTab([multiSourceIdentifier()]);

    await user.click(screen.getByRole("button", { name: "Show details" }));

    expect(screen.getByText("Source records (3)")).toBeTruthy();
    expect(screen.getByText("sr-1")).toBeTruthy();
    expect(screen.getByText("sr-2")).toBeTruthy();
    expect(screen.getByText("sr-3")).toBeTruthy();
  });

  it("falls back to the identifier provenance key when no records carry a system", () => {
    renderTab([
      identifier({
        identifier_type: "email",
        normalized_value: "customer@example.test",
        source_system_key: "sgbankruptcy",
      }),
    ]);

    expect(screen.getByText("sgbankruptcy")).toBeTruthy();
  });

  it("masks NRIC values until revealed", () => {
    renderTab([identifier({ identifier_type: "nric", normalized_value: "S9436749B" })]);

    expect(screen.getByText("S*****49B")).toBeTruthy();
    expect(screen.queryByText("S9436749B")).toBeNull();
  });
});
