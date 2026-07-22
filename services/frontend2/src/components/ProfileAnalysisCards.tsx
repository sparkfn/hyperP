import React, { type ReactElement } from "react";

import type {
  PersonProfileAnalyses,
  ProfileAnalysisSlot,
  ProfileAnalysisSlotRefreshState,
} from "../lib/api-types-person";
import styles from "./ProfileAnalysis.module.css";

interface ProfileAnalysisCardsProps {
  analyses: PersonProfileAnalyses;
}

interface AnalysisCardProps {
  title: string;
  slot: ProfileAnalysisSlot;
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
  return "No analysis is available yet.";
}

function AnalysisCard({ title, slot }: AnalysisCardProps): ReactElement {
  const current = slot.current;
  return (
    <article className={styles.card}>
      <div className={styles.cardHeader}>
        <h3 className={styles.cardTitle}>{title}</h3>
        <div className={styles.badges} aria-label={`${title} status`}>
          {slot.stale && <span className={`${styles.badge} ${styles.staleBadge}`}>Stale</span>}
          <span className={`${styles.badge} ${styles[`state_${slot.refresh_state}`]}`}>
            {slotStateLabel(slot.refresh_state, current !== null)}
          </span>
        </div>
      </div>

      {slot.refresh_state === "failed" && slot.failure_code !== null && (
        <p className={styles.failureCode}>Failure code: {slot.failure_code}</p>
      )}

      {current === null ? (
        <p className={styles.emptyState}>{missingOutputMessage(slot.refresh_state)}</p>
      ) : (
        <>
          <div className={styles.content}>{current.content}</div>
          <div className={styles.metadata}>
            <span>Generated {current.completed_at_display}</span>
            <span>Model {current.model}</span>
          </div>
        </>
      )}
    </article>
  );
}

export default function ProfileAnalysisCards({
  analyses,
}: ProfileAnalysisCardsProps): ReactElement {
  return (
    <div className={styles.cardsArea}>
      <p className={styles.overallState} aria-live="polite">
        Profile analysis status: <span>{OVERALL_LABELS[analyses.refresh_state]}</span>
      </p>
      <div className={styles.cardGrid}>
        <AnalysisCard title="Sales" slot={analyses.sales} />
        <AnalysisCard title="Contact tracing" slot={analyses.contact_tracing} />
      </div>
    </div>
  );
}
