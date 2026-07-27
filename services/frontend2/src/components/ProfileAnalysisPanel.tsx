"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import React, { type ReactElement, useCallback, useEffect, useRef, useState } from "react";

import { BffError, bffFetch } from "../lib/api-client";
import type {
  PersonProfileAnalyses,
  ProfileAnalysisRequestResult,
  ProfileAnalysisType,
} from "../lib/api-types-person";
import { parsePersonProfileAnalyses } from "../lib/profile-analysis-contracts";
import ProfileAnalysisCards from "./ProfileAnalysisCards";
import styles from "./ProfileAnalysis.module.css";

interface ProfileAnalysisPanelProps {
  personId: string;
}

const PROFILE_ANALYSIS_POLL_INTERVAL_MS = 5_000;
const PROFILE_ANALYSIS_IDLE_REFRESH_INTERVAL_MS = 60_000;

function isRefreshActive(analyses: PersonProfileAnalyses): boolean {
  return [analyses.sales.refresh_state, analyses.contact_tracing.refresh_state].some(
    (state) => state === "pending" || state === "running" || state === "retrying",
  );
}

function requestKey(
  personId: string,
  analysisType: ProfileAnalysisType,
  inputRevision: number,
): string {
  return `${personId}:${analysisType}:${inputRevision}`;
}

export default function ProfileAnalysisPanel({
  personId,
}: ProfileAnalysisPanelProps): ReactElement {
  const queryClient = useQueryClient();
  const automaticRequests = useRef(new Set<string>());
  const [requestingTypes, setRequestingTypes] = useState<ReadonlySet<ProfileAnalysisType>>(
    new Set(),
  );
  const [requestError, setRequestError] = useState<string | null>(null);
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
      if (analyses === undefined) return false;
      return isRefreshActive(analyses)
        ? PROFILE_ANALYSIS_POLL_INTERVAL_MS
        : PROFILE_ANALYSIS_IDLE_REFRESH_INTERVAL_MS;
    },
  });

  const requestAnalysis = useCallback(async (
    analysisType: ProfileAnalysisType,
    force: boolean,
  ): Promise<void> => {
    if (requestingTypes.has(analysisType)) return;
    setRequestError(null);
    setRequestingTypes((current) => new Set([...current, analysisType]));
    try {
      const result = await bffFetch<ProfileAnalysisRequestResult>(
        `/bff/persons/${encodeURIComponent(personId)}/profile-analyses`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ analysis_type: analysisType, force }),
        },
      );
      if (result.state === "force_limited") {
        setRequestError(
          result.force_available_at_display === null
            ? "The forced refresh limit has been reached."
            : `The forced refresh limit has been reached until ${result.force_available_at_display}.`,
        );
      }
      await queryClient.invalidateQueries({ queryKey: ["person-profile-analyses", personId] });
    } catch (error) {
      const forceAvailableAt = error instanceof BffError
        ? error.details?.force_available_at_display
        : undefined;
      setRequestError(
        forceAvailableAt === undefined
          ? (error instanceof BffError
            ? error.message
            : "Profile analysis could not be requested.")
          : `The forced refresh limit has been reached until ${forceAvailableAt}.`,
      );
    } finally {
      setRequestingTypes((current) => {
        const next = new Set(current);
        next.delete(analysisType);
        return next;
      });
    }
  }, [personId, queryClient, requestingTypes]);

  useEffect(() => {
    if (query.data === undefined) return;
    const slots: ReadonlyArray<readonly [ProfileAnalysisType, boolean]> = [
      ["sales", query.data.sales.auto_request_allowed],
      ["contact_tracing", query.data.contact_tracing.auto_request_allowed],
    ];
    for (const [analysisType, allowed] of slots) {
      const key = requestKey(personId, analysisType, query.data.input_revision);
      if (!allowed) {
        automaticRequests.current.delete(key);
        continue;
      }
      if (!automaticRequests.current.has(key)) {
        automaticRequests.current.add(key);
        void requestAnalysis(analysisType, false);
      }
    }
  }, [personId, query.data, requestAnalysis]);

  const handleForceRequest = useCallback((analysisType: ProfileAnalysisType): void => {
    const accepted = window.confirm(
      "This analysis is still valid. Generate a new version? "
      + "You can force up to three refreshes per hour for this person and analysis type.",
    );
    if (accepted) void requestAnalysis(analysisType, true);
  }, [requestAnalysis]);

  if (query.data === undefined && query.isPending) {
    return (
      <div className={styles.loadingState} role="status" aria-busy="true">
        <span className={styles.loadingOverlay}>Loading profile analysis…</span>
      </div>
    );
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
    <div className={styles.panel} aria-busy={requestingTypes.size > 0}>
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
      {requestError !== null && <div className={styles.errorState} role="alert">{requestError}</div>}
      <ProfileAnalysisCards
        analyses={query.data}
        requestingTypes={requestingTypes}
        onForceRequest={handleForceRequest}
      />
    </div>
  );
}
