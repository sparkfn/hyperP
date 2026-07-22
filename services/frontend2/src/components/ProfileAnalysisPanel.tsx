"use client";

import { useQuery } from "@tanstack/react-query";
import React, { type ReactElement } from "react";

import { BffError, bffFetch } from "../lib/api-client";
import type { PersonProfileAnalyses } from "../lib/api-types-person";
import { parsePersonProfileAnalyses } from "../lib/profile-analysis-contracts";
import ProfileAnalysisCards from "./ProfileAnalysisCards";
import styles from "./ProfileAnalysis.module.css";

interface ProfileAnalysisPanelProps {
  personId: string;
}

const PROFILE_ANALYSIS_POLL_INTERVAL_MS = 5_000;

function isRefreshActive(analyses: PersonProfileAnalyses): boolean {
  return [analyses.sales.refresh_state, analyses.contact_tracing.refresh_state].some(
    (state) => state === "pending" || state === "running" || state === "retrying",
  );
}

export default function ProfileAnalysisPanel({
  personId,
}: ProfileAnalysisPanelProps): ReactElement {
  const query = useQuery<PersonProfileAnalyses, Error>({
    queryKey: ["person-profile-analyses", personId],
    queryFn: async ({ signal }): Promise<PersonProfileAnalyses> => {
      const value: unknown = await bffFetch<unknown>(
        `/bff/persons/${encodeURIComponent(personId)}/profile-analyses`,
        { signal },
      );
      return parsePersonProfileAnalyses(value, personId);
    },
    refetchInterval: (currentQuery) => {
      const analyses = currentQuery.state.data;
      return analyses !== undefined && isRefreshActive(analyses)
        ? PROFILE_ANALYSIS_POLL_INTERVAL_MS
        : false;
    },
  });

  if (query.data === undefined && query.isPending) {
    return <div className={styles.loadingState} role="status">Loading profile analysis…</div>;
  }

  if (query.data === undefined) {
    const message = query.error instanceof BffError
      ? query.error.message
      : "Profile analysis could not be loaded.";
    return <div className={styles.errorState} role="alert">{message}</div>;
  }

  const refreshWarning = query.isError
    ? query.error instanceof BffError
      ? query.error.message
      : "Profile analysis refresh could not be completed."
    : null;

  return (
    <div className={styles.panel}>
      {refreshWarning !== null && (
        <div
          className={styles.refreshWarning}
          role="status"
          aria-label="Profile analysis refresh warning"
          aria-live="polite"
        >
          {refreshWarning} Showing the last available analysis.
        </div>
      )}
      <ProfileAnalysisCards analyses={query.data} />
    </div>
  );
}
