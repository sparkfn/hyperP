import React, { type ReactElement } from "react";

import type {
  PersonProfileAnalyses,
  ProfileAnalysisSlot,
  ProfileAnalysisSlotRefreshState,
  ProfileAnalysisType,
} from "../lib/api-types-person";
import styles from "./ProfileAnalysis.module.css";

interface ProfileAnalysisCardsProps {
  analyses: PersonProfileAnalyses;
  requestingTypes: ReadonlySet<ProfileAnalysisType>;
  onForceRequest: (analysisType: ProfileAnalysisType) => void;
  onRetryRequest: (analysisType: ProfileAnalysisType) => void;
}

interface AnalysisCardProps {
  title: string;
  analysisType: ProfileAnalysisType;
  slot: ProfileAnalysisSlot;
  requesting: boolean;
  onForceRequest: (analysisType: ProfileAnalysisType) => void;
  onRetryRequest: (analysisType: ProfileAnalysisType) => void;
}

const OVERALL_LABELS = {
  disabled: "Generation paused",
  pending: "Queued",
  running: "Generating",
  retrying: "Retry scheduled",
  ready: "Available",
  partial: "Partially available",
  failed: "Generation failed",
} as const;

function slotStateLabel(state: ProfileAnalysisSlotRefreshState, hasCurrent: boolean): string {
  if (state === "disabled") return "Generation paused";
  if (state === "idle") return hasCurrent ? "Update available" : "Not generated";
  if (state === "pending") return hasCurrent ? "Refresh queued" : "Queued";
  if (state === "running") return hasCurrent ? "Refreshing" : "Generating";
  if (state === "retrying") return hasCurrent ? "Refresh retry scheduled" : "Retry scheduled";
  if (state === "failed") return hasCurrent ? "Refresh failed" : "Generation failed";
  return "Up to date";
}

function missingOutputMessage(state: ProfileAnalysisSlotRefreshState): string {
  if (state === "disabled") return "Analysis generation is currently paused.";
  if (state === "pending") return "Analysis is queued.";
  if (state === "running") return "Analysis is being generated.";
  if (state === "retrying") return "Analysis will retry automatically.";
  if (state === "failed") return "Analysis is unavailable because generation failed.";
  return "An analysis will be generated when this profile is opened.";
}

function invalidityMessage(slot: ProfileAnalysisSlot): string | null {
  if (slot.stale && slot.expired) return "Profile data changed and this analysis has expired.";
  if (slot.stale) return "Profile data changed after this analysis was generated.";
  if (slot.expired) return "This analysis has expired and is being updated.";
  return null;
}

function AnalysisCard({
  title,
  analysisType,
  slot,
  requesting,
  onForceRequest,
  onRetryRequest,
}: AnalysisCardProps): ReactElement {
  const current = slot.current;
  const invalidity = invalidityMessage(slot);
  const active = requesting || ["pending", "running", "retrying"].includes(slot.refresh_state);
  const canForce = slot.valid && !active && slot.refresh_state !== "disabled";
  const forceLimited = slot.force_attempts_remaining === 0;
  const retryLimited = slot.retry_attempts_remaining === 0;
  return (
    <article className={styles.card} aria-busy={active}>
      <div className={styles.cardHeader}>
        <h3 className={styles.cardTitle}>{title}</h3>
        <div className={styles.badges} aria-label={`${title} status`}>
          {slot.stale && <span className={`${styles.badge} ${styles.staleBadge}`}>Stale</span>}
          {slot.expired && <span className={`${styles.badge} ${styles.staleBadge}`}>Expired</span>}
          <span className={`${styles.badge} ${styles[`state_${slot.refresh_state}`]}`}>
            {slotStateLabel(slot.refresh_state, current !== null)}
          </span>
        </div>
      </div>

      {invalidity !== null && <p className={styles.invalidity}>{invalidity}</p>}
      {slot.refresh_state === "failed" && slot.failure_code !== null && (
        <p className={styles.failureCode}>Latest refresh failed: {slot.failure_code}</p>
      )}

      {current === null ? (
        <p className={styles.emptyState}>{missingOutputMessage(slot.refresh_state)}</p>
      ) : (
        <>
          <div className={styles.content}>{current.content}</div>
          <div className={styles.metadata}>
            <span>Generated {current.generated_age_display}</span>
            <span>{current.completed_at_display}</span>
            <span>Valid until {current.valid_until_display}</span>
            <span>Model {current.model}</span>
          </div>
        </>
      )}

      {canForce && (
        <div className={styles.actions}>
          <button
            className={styles.forceButton}
            type="button"
            disabled={forceLimited}
            onClick={() => onForceRequest(analysisType)}
          >
            Force new analysis
          </button>
          <span className={styles.forceHelp}>
            {forceLimited && slot.force_available_at_display !== null
              ? `Forced refreshes available again ${slot.force_available_at_display}`
              : `${slot.force_attempts_remaining} forced refreshes available this hour`}
          </span>
        </div>
      )}
      {slot.refresh_state === "failed" && (
        <div className={styles.actions}>
          <button
            className={styles.forceButton}
            type="button"
            disabled={requesting || !slot.retry_allowed}
            onClick={() => onRetryRequest(analysisType)}
          >
            Retry analysis
          </button>
          <span className={styles.forceHelp}>
            {retryLimited && slot.retry_available_at_display !== null
              ? `Retries available again ${slot.retry_available_at_display}`
              : `${slot.retry_attempts_remaining} ${
                slot.retry_attempts_remaining === 1 ? "retry" : "retries"
              } available this hour for this person`}
          </span>
        </div>
      )}
      {active && <div className={styles.cardOverlay} role="status">Generating updated analysis…</div>}
    </article>
  );
}

export default function ProfileAnalysisCards({
  analyses,
  requestingTypes,
  onForceRequest,
  onRetryRequest,
}: ProfileAnalysisCardsProps): ReactElement {
  return (
    <div className={styles.cardsArea}>
      <p className={styles.overallState} aria-live="polite">
        Profile analysis status: <span>{OVERALL_LABELS[analyses.refresh_state]}</span>
      </p>
      <div className={styles.cardGrid}>
        <AnalysisCard
          title="Sales"
          analysisType="sales"
          slot={analyses.sales}
          requesting={requestingTypes.has("sales")}
          onForceRequest={onForceRequest}
          onRetryRequest={onRetryRequest}
        />
        <AnalysisCard
          title="Contact tracing"
          analysisType="contact_tracing"
          slot={analyses.contact_tracing}
          requesting={requestingTypes.has("contact_tracing")}
          onForceRequest={onForceRequest}
          onRetryRequest={onRetryRequest}
        />
      </div>
    </div>
  );
}
