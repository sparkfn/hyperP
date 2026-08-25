"use client";

import { useCallback, useEffect, useState, type ReactElement } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import ActionToast, { type ToastState } from "@/components/ActionToast";
import MergeOverlay from "@/components/MergeOverlay";
import ReviewActionsPanel from "@/components/ReviewActionsPanel";
import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonComparisonEntity, ReviewCaseActionEntry, ReviewCaseDetail } from "@/lib/api-types-ops";
import type { PersonSourceRecord, SharedIdentifierGroup } from "@/lib/api-types-person";
import { completenessColor, formatDate, formatDob, relativeTime } from "@/lib/display";
import { toBasePath } from "@/lib/route-paths";
import { sourceRecordReference } from "@/lib/ui-display";
import styles from "../review.module.css";

function queueStateColor(state: string): string {
  switch (state) {
    case "resolved": return "#22c55e";
    case "assigned": return "#3b82f6";
    case "deferred": return "#f59e0b";
    case "cancelled": return "#94a3b8";
    default: return "#ef4444";
  }
}

function titleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function shortCaseId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}

function subjectInitials(value: string): string {
  return value
    .split(/\s+|↔/)
    .filter(Boolean)
    .map((part) => part.charAt(0))
    .slice(0, 2)
    .join("")
    .toUpperCase() || "R";
}

function subjectAvatarColor(value: string): string {
  const palette = ["#ef4444", "#f97316", "#8b5cf6", "#06b6d4", "#10b981", "#6366f1", "#ec4899"];
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  return palette[hash % palette.length] ?? "#6366f1";
}

export function reviewSubjectTitle(detail: ReviewCaseDetail): string {
  const leftName = detail.comparison_left?.preferred_full_name ?? null;
  const rightName = detail.comparison_right?.preferred_full_name ?? null;
  if (leftName !== null && rightName !== null) return `${leftName} ↔ ${rightName}`;
  if (leftName !== null) return leftName;
  if (rightName !== null) return rightName;
  return `${titleCase(detail.match_decision.decision)} case`;
}

export function reviewSubjectSubtitle(detail: ReviewCaseDetail): string {
  const leftPersonId = detail.comparison_left?.person_id ?? null;
  const rightPersonId = detail.comparison_right?.person_id ?? null;
  if (leftPersonId !== null && rightPersonId !== null) return `Pair audit · Case ref: ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
  if (leftPersonId !== null || rightPersonId !== null) return `Person review · Case ref: ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
  return `Case ref: ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
}

function KeyValue({ label, value }: { label: string; value: string }): ReactElement {
  return (
    <div className={styles.kvItem}>
      <span className={styles.kvLabel}>{label}</span>
      <span className={styles.kvValue}>{value}</span>
    </div>
  );
}

const compactSvg = { width: 12, height: 12, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };

function ComparisonCard({ title, entity }: { title: string; entity: PersonComparisonEntity | null }): ReactElement {
  if (entity === null) {
    return (
      <section className={`${styles.detailCard} ${styles.comparisonCard}`}>
        <div className={styles.comparisonLabel}>{title}</div>
        <div className={styles.emptyCompact}>No comparison entity.</div>
      </section>
    );
  }

  const personHref = entity.person_id !== null ? toBasePath(`/persons/${encodeURIComponent(entity.person_id)}`) : null;
  const sourceReference = entity.source_record_id === null
    ? null
    : sourceRecordReference(entity.source_record_id, entity.source_system_key ?? undefined);
  const displayName = entity.preferred_full_name ?? sourceReference ?? "Unknown";
  const entityLabel = titleCase(entity.entity_kind);
  const idLabel = entity.person_id ?? sourceReference ?? "—";

  return (
    <section className={`${styles.detailCard} ${styles.comparisonCard}`}>
      <div className={styles.comparisonLabel}>{title}</div>
      <div className={styles.comparisonHeader}>
        <span className={styles.caseAvatar} style={{ background: subjectAvatarColor(displayName) }}>
          {subjectInitials(displayName)}
        </span>
        <div className={styles.comparisonCopy}>
          <div className={styles.comparisonName}>{displayName}</div>
          <div className={styles.caseMeta}>{entityLabel} · {idLabel}</div>
        </div>
      </div>
      <div className={styles.comparisonFields}>
        <KeyValue label="Status" value={entity.status ?? "—"} />
        <KeyValue label="Phone" value={entity.preferred_phone ?? "—"} />
        <KeyValue label="Email" value={entity.preferred_email ?? "—"} />
        <KeyValue label="DOB" value={formatDob(entity.preferred_dob)} />
        <KeyValue label="Address" value={entity.preferred_address?.normalized_full ?? "—"} />
        {entity.entity_kind === "source_record" && entity.source_system_key !== null ? (
          <KeyValue label="Source" value={entity.source_system_key} />
        ) : null}
        {entity.entity_kind === "source_record" && entity.observed_at !== null ? (
          <KeyValue label="Observed" value={formatDate(entity.observed_at) || "—"} />
        ) : null}
        <KeyValue label="Source record" value={sourceReference ?? "—"} />
      </div>
      {personHref !== null ? <Link className={styles.profileLinkButton} href={personHref}>Open person profile</Link> : null}
    </section>
  );
}

interface SourceRow {
  key: string;
  label: string;
  observed: string;
}

function toSourceRows(records: PersonSourceRecord[]): SourceRow[] {
  const latest = new Map<string, SourceRow>();
  for (const record of records) {
    const key = record.source_system ?? record.record_type ?? record.source_record_pk;
    const label = record.entity_display_name ?? record.source_system ?? (record.record_type ? titleCase(record.record_type) : null) ?? "source";
    const observed = record.observed_at ?? "";
    const existing = latest.get(key);
    if (existing === undefined || (observed !== "" && observed > existing.observed)) {
      latest.set(key, { key, label, observed });
    }
  }
  return [...latest.values()];
}

function SourceColumn({ heading, rows }: { heading: string; rows: SourceRow[] }): ReactElement {
  return (
    <div className={styles.evidenceCol}>
      <span className={styles.evidenceColHead}>{heading}</span>
      {rows.length === 0 ? (
        <span className={styles.evidenceEmpty}>No source record</span>
      ) : (
        rows.map((row) => (
          <span key={row.key} className={styles.evidenceRow}>
            <span className={styles.evidenceSource}>{row.label}</span>
            {row.observed !== "" ? (
              <span className={styles.evidenceObserved}>{formatDate(row.observed)}</span>
            ) : null}
          </span>
        ))
      )}
    </div>
  );
}

function EvidenceSection({ groups }: { groups: SharedIdentifierGroup[] }): ReactElement {
  const current = toSourceRows(groups.flatMap((group) => group.current_person_source_records));
  const candidate = toSourceRows(groups.flatMap((group) => group.candidate_source_records));
  if (current.length === 0 && candidate.length === 0) {
    return <></>;
  }
  return (
    <section className={styles.detailCard}>
      <div className={styles.cardHeader}>Source · where the data came from</div>
      <div className={styles.evidenceCols}>
        <SourceColumn heading="Current profile" rows={current} />
        <SourceColumn heading="Candidate evidence" rows={candidate} />
      </div>
    </section>
  );
}

function parseReason(reason: string): { label: string; score: string; note: string | null } | null {
  // API returns strings like "Phone match (unverified: +0.20)" or
  // "Medium name similarity (0.63: +0.10)" or
  // "Shared phone links 2 active persons (uuid1, uuid2)" (no score).
  // We extract label, score, and note so the chip can render as
  // "Phone match +20% (unverified)".
  const lastClose = reason.lastIndexOf(")");
  if (lastClose <= 0 || !reason.endsWith(")")) return null;
  const firstOpen = reason.indexOf("(");
  if (firstOpen < 0) return null;
  const label = reason.slice(0, firstOpen).trim();
  const inner = reason.slice(firstOpen + 1, lastClose).trim();
  const scoreMatch = /([+-]\d+\.\d+)\s*$/.exec(inner);
  if (scoreMatch === null) return null;
  const contributionStr = scoreMatch[1];
  if (contributionStr === undefined) return null;
  const note = inner.slice(0, inner.length - contributionStr.length).replace(/:\s*$/, "").trim();
  const pct = Math.round(parseFloat(contributionStr) * 100);
  const score = `${pct >= 0 ? "+" : ""}${pct}%`;
  return {
    label: label.length > 0 ? label : reason,
    score,
    note: note.length > 0 ? note : null,
  };
}

function ActionEntry({ action }: { action: ReviewCaseActionEntry }): ReactElement {
  const type = action.action_type ?? "action";
  const actor = action.actor_id !== null && action.actor_id !== undefined ? ` — ${action.actor_id}` : "";
  const created = action.created_at !== null && action.created_at !== undefined ? ` @ ${action.created_at}` : "";
  return (
    <div className={styles.actionItem}>
      <div className={styles.actionTitle}>{type}{actor}{created}</div>
      {action.notes !== null && action.notes !== undefined && action.notes !== "" ? (
        <div className={styles.detailMeta}>{action.notes}</div>
      ) : null}
    </div>
  );
}

export function ReviewCaseDetailContent({
  detail,
  loading,
  error,
  onChanged,
  compact = false,
  onActionBusy,
  onActionDone,
}: {
  detail: ReviewCaseDetail;
  loading: boolean;
  error: string | null;
  onChanged: () => Promise<void>;
  compact?: boolean;
  onActionBusy?: (busy: boolean) => void;
  onActionDone?: (success: boolean, message: string) => void;
}): ReactElement {
  const confidence = detail.match_decision.confidence;
  const leftPersonId = detail.comparison_left?.person_id ?? null;
  const rightPersonId = detail.comparison_right?.person_id ?? null;
  const subjectTitle = reviewSubjectTitle(detail);
  const subjectSubtitle = reviewSubjectSubtitle(detail);
  void loading;

  const decisionCard = (
    <section className={styles.detailCard}>
      <div className={styles.cardHeader}>Decision</div>
      <div className={styles.kvGridCompact}>
        <KeyValue label="Outcome" value={detail.match_decision.decision} />
        <KeyValue label="Engine" value={detail.match_decision.engine_type} />
        <KeyValue label="Assigned to" value={detail.assigned_to ?? "—"} />
        <KeyValue label="Follow-up" value={formatDate(detail.follow_up_at ?? "") || "—"} />
        <KeyValue label="SLA" value={formatDate(detail.sla_due_at ?? "") || "—"} />
        <KeyValue label="Resolution" value={detail.resolution ?? "—"} />
      </div>
      <div className={styles.reasonBox}>
        <div className={styles.reasonBoxLabel}>Reasons</div>
        {(() => {
          const parsed = detail.match_decision.reasons
            .map(parseReason)
            .filter((r): r is { label: string; score: string; note: string | null } => r !== null);
          if (parsed.length === 0) return <span>{detail.match_decision.reasons.join(" · ") || "No reasons recorded."}</span>;
          return (
            <div className={styles.reasonChips}>
              {parsed.map((r, i) => (
                <span key={i} className={styles.reasonChip}>
                  {r.label} {r.score}
                  {r.note !== null ? <span className={styles.reasonChipNote}> ({r.note})</span> : null}
                </span>
              ))}
            </div>
          );
        })()}
      </div>
    </section>
  );

  const actionHistoryCard = (
    <section className={styles.detailCard}>
      <div className={styles.cardHeader}>Action history</div>
      {detail.actions.length === 0 ? (
        <div className={styles.emptyCompact}>No actions yet.</div>
      ) : (
        <div className={styles.actionList}>
          {detail.actions.map((action, index) => <ActionEntry key={index} action={action} />)}
        </div>
      )}
    </section>
  );

  const actionsPanel = (
    <ReviewActionsPanel
      reviewCaseId={detail.review_case_id}
      queueState={detail.queue_state}
      assignedTo={detail.assigned_to}
      leftPersonId={leftPersonId}
      rightPersonId={rightPersonId}
      reviewCandidatePersonIds={detail.match_decision.review_candidate_person_ids}
      leftPersonStatus={detail.comparison_left?.status ?? null}
      rightPersonStatus={detail.comparison_right?.status ?? null}
      leftLabel={compact ? "Current profile" : undefined}
      rightLabel={compact ? "Candidate evidence" : undefined}
      onChanged={onChanged}
      onActionBusy={onActionBusy}
      onActionDone={onActionDone}
    />
  );

  return (
    <>
      <section className={styles.detailHero}>
        <div className={styles.detailSubjectBlock}>
          <span className={styles.caseAvatar} style={{ background: subjectAvatarColor(subjectTitle) }}>
            {subjectInitials(subjectTitle)}
          </span>
          <div className={styles.detailSubjectCopy}>
            <div className={styles.eyebrow}>Review case</div>
            <h1 className={styles.detailTitle}>{subjectTitle}</h1>
            <div className={styles.caseMeta}>{subjectSubtitle}</div>
            <div className={styles.detailMeta}>
              Created {formatDate(detail.created_at)} · Updated {relativeTime(detail.updated_at)}
            </div>
          </div>
        </div>
        <div className={styles.heroStats}>
          <span className={styles.badge} style={{ color: queueStateColor(detail.queue_state), background: `${queueStateColor(detail.queue_state)}18` }}>
            {detail.queue_state}
          </span>
          <span className={styles.badge}>Priority {detail.priority}</span>
          <span className={styles.badge} style={{ color: completenessColor(confidence), background: `${completenessColor(confidence)}18` }}>
            {Math.round(confidence * 100)}% confidence
          </span>
        </div>
      </section>

      {error !== null ? <div className={styles.errorBanner}>{error}</div> : null}

      {compact ? (
        // Single-column modal layout: Decision → Evidence → Reviewer Actions → Action history
        <div className={styles.detailMainColumn}>
          {decisionCard}
          {detail.shared_identifier_groups.length > 0 ? (
            <EvidenceSection groups={detail.shared_identifier_groups} />
          ) : null}
          {actionsPanel}
          {actionHistoryCard}
        </div>
      ) : (
        // Full-page two-column layout with comparison cards
        <div className={styles.detailBody}>
          <div className={styles.detailMainColumn}>
            {decisionCard}
            <div className={styles.compareGrid}>
              <ComparisonCard title="Left" entity={detail.comparison_left} />
              <ComparisonCard title="Right" entity={detail.comparison_right} />
            </div>
            {detail.shared_identifier_groups.length > 0 ? (
              <EvidenceSection groups={detail.shared_identifier_groups} />
            ) : null}
            {actionHistoryCard}
          </div>
          <aside className={styles.reviewActionRail}>
            {actionsPanel}
          </aside>
        </div>
      )}
    </>
  );
}

export function ReviewCaseDetailModal({
  open,
  reviewCaseId,
  onClose,
  onActionComplete,
}: {
  open: boolean;
  reviewCaseId: string;
  onClose: () => void;
  onActionComplete?: () => void;
}): ReactElement | null {
  const [detail, setDetail] = useState<ReviewCaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const router = useRouter();

  const loadDetail = useCallback((): Promise<void> => {
    if (reviewCaseId.length === 0) {
      setError("Review case id is missing.");
      setLoading(false);
      return Promise.resolve();
    }
    setLoading(true);
    setError(null);
    return bffFetch<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}`)
      .then((res) => setDetail(res))
      .catch((err: unknown) => {
        setError(err instanceof BffError ? err.message : "Failed to load review case.");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [reviewCaseId]);

  useEffect(() => {
    if (!open) {
      setDetail(null);
      setError(null);
      return;
    }
    queueMicrotask(loadDetail);
  }, [open, loadDetail]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className={styles.reviewCaseOverlay} onClick={onClose}>
      <div className={`${styles.reviewCaseModal} ${styles.reviewCaseModalNarrow}`} onClick={(e) => e.stopPropagation()}>
        {actionBusy && <MergeOverlay label="Processing…" />}
        <div className={styles.reviewCaseModalHeader}>
          <Link href={`/review/${encodeURIComponent(reviewCaseId)}`} className={styles.reviewCaseOpenFull} target="_blank" rel="noopener noreferrer">
            Open in full page ↗
          </Link>
          <button type="button" className={styles.reviewCaseModalClose} onClick={onClose} aria-label="Close">×</button>
        </div>
        {loading && detail === null ? (
          <div className={styles.reviewCaseModalLoading}>
            <MergeOverlay label="Loading…" />
          </div>
        ) : error !== null && detail === null ? (
          <div className={styles.reviewCaseModalError}>{error}</div>
        ) : detail !== null ? (
          <ReviewCaseDetailContent
            detail={detail}
            loading={loading}
            error={error}
            onChanged={async () => {
              await new Promise<void>((resolve) => setTimeout(resolve, 1500));
              await loadDetail();
              router.refresh();
              onActionComplete?.();
            }}
            compact
            onActionBusy={setActionBusy}
            onActionDone={(success, message) => setToast({ type: success ? "success" : "error", message })}
          />
        ) : null}
      </div>
      {toast !== null && (
        <ActionToast
          type={toast.type}
          message={toast.message}
          onDismiss={() => setToast(null)}
        />
      )}
    </div>
  );
}
