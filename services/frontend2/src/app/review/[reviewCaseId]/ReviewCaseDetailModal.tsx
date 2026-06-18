"use client";

import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import Link from "next/link";

import ReviewActionsPanel from "@/components/ReviewActionsPanel";
import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonComparisonEntity, ReviewCaseActionEntry, ReviewCaseDetail } from "@/lib/api-types-ops";
import { completenessColor, formatDate, isOverdue, relativeTime } from "@/lib/display";
import { toBasePath } from "@/lib/route-paths";
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
  if (leftPersonId !== null && rightPersonId !== null) return `Pair audit · ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
  if (leftPersonId !== null || rightPersonId !== null) return `Person review · ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
  return `${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
}

function KeyValue({ label, value }: { label: string; value: string }): ReactElement {
  return (
    <div className={styles.kvItem}>
      <span className={styles.kvLabel}>{label}</span>
      <span className={styles.kvValue}>{value}</span>
    </div>
  );
}

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
  const displayName = entity.preferred_full_name ?? entity.source_record_id ?? "Unknown";
  const entityLabel = titleCase(entity.entity_kind);
  const idLabel = entity.person_id ?? entity.source_record_id ?? entity.source_record_pk ?? "—";
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
        <KeyValue label="DOB" value={entity.preferred_dob ?? "—"} />
        <KeyValue label="Address" value={entity.preferred_address?.normalized_full ?? "—"} />
        <KeyValue label="Source record" value={entity.source_record_id ?? entity.source_record_pk ?? "—"} />
      </div>
      {personHref !== null ? <Link className={styles.profileLinkButton} href={personHref}>Open person profile</Link> : null}
    </section>
  );
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
  defaultSurvivorPersonId,
}: {
  detail: ReviewCaseDetail;
  loading: boolean;
  error: string | null;
  onChanged: () => Promise<void>;
  defaultSurvivorPersonId?: string | null;
}): ReactElement {
  const confidence = detail.match_decision.confidence;
  const leftPersonId = detail.comparison_left?.person_id ?? null;
  const rightPersonId = detail.comparison_right?.person_id ?? null;
  const subjectTitle = reviewSubjectTitle(detail);
  const subjectSubtitle = reviewSubjectSubtitle(detail);
  const slaOverdue = isOverdue(detail.sla_due_at);
  void loading;

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

      <div className={styles.detailBody}>
        <div className={styles.detailMainColumn}>
          <section className={styles.detailCard}>
            <div className={styles.cardHeader}>Decision</div>
            <div className={styles.kvGrid}>
              <KeyValue label="Decision" value={detail.match_decision.decision} />
              <KeyValue label="Engine" value={detail.match_decision.engine_type} />
              <KeyValue label="Assigned to" value={detail.assigned_to ?? "—"} />
              <KeyValue label="Follow-up" value={formatDate(detail.follow_up_at)} />
              <div className={styles.kvItem}>
                <span className={styles.kvLabel}>SLA</span>
                <span className={styles.kvValue} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  {detail.sla_due_at
                    ? (
                      <>
                        <span className={slaOverdue ? styles.slaOverdue : undefined}>
                          {formatDate(detail.sla_due_at)}
                        </span>
                        {slaOverdue && (
                          <span className={styles.badge} style={{ background: "var(--bad-bg, #fef2f2)", color: "var(--bad, #ef4444)" }}>overdue</span>
                        )}
                      </>
                    )
                    : "—"}
                </span>
              </div>
              <KeyValue label="Resolution" value={detail.resolution ?? "—"} />
            </div>
            <div className={styles.reasonBox}>{detail.match_decision.reasons.join(" · ") || "No reasons recorded."}</div>
          </section>

          <div className={styles.compareGrid}>
            <ComparisonCard title="Left" entity={detail.comparison_left} />
            <ComparisonCard title="Right" entity={detail.comparison_right} />
          </div>

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
        </div>

        <aside className={styles.reviewActionRail}>
          <ReviewActionsPanel
            reviewCaseId={detail.review_case_id}
            queueState={detail.queue_state}
            assignedTo={detail.assigned_to}
            leftPersonId={leftPersonId}
            rightPersonId={rightPersonId}
            leftPersonStatus={detail.comparison_left?.status ?? null}
            rightPersonStatus={detail.comparison_right?.status ?? null}
            defaultSurvivorPersonId={defaultSurvivorPersonId}
            onChanged={onChanged}
          />
        </aside>
      </div>
    </>
  );
}

export function ReviewCaseDetailModal({
  open,
  reviewCaseId,
  onClose,
  defaultSurvivorPersonId,
}: {
  open: boolean;
  reviewCaseId: string;
  onClose: (actioned: boolean) => void;
  defaultSurvivorPersonId?: string | null;
}): ReactElement | null {
  const [detail, setDetail] = useState<ReviewCaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const actioned = useRef(false);

  const loadDetail = useCallback(async (): Promise<void> => {
    if (reviewCaseId.length === 0) {
      setError("Review case id is missing.");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await bffFetch<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}`);
      setDetail(res);
    } catch (err: unknown) {
      setError(err instanceof BffError ? err.message : "Failed to load review case.");
    } finally {
      setLoading(false);
    }
  }, [reviewCaseId]);

  const handleChanged = useCallback(async (): Promise<void> => {
    actioned.current = true;
    await loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    if (!open) {
      setDetail(null);
      setError(null);
      return;
    }
    actioned.current = false;
    queueMicrotask(loadDetail);
  }, [open, loadDetail]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose(actioned.current);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className={styles.reviewCaseOverlay} onClick={() => onClose(actioned.current)}>
      <div className={styles.reviewCaseModal} onClick={(e) => e.stopPropagation()}>
        <div className={styles.reviewCaseModalHeader}>
          <Link href={`/review/${encodeURIComponent(reviewCaseId)}`} className={styles.reviewCaseOpenFull} target="_blank" rel="noopener noreferrer">
            Open in full page ↗
          </Link>
          <button type="button" className={styles.reviewCaseModalClose} onClick={() => onClose(actioned.current)} aria-label="Close">×</button>
        </div>
        {loading && detail === null ? (
          <div className={styles.reviewCaseModalLoading}>Loading review case…</div>
        ) : error !== null && detail === null ? (
          <div className={styles.reviewCaseModalError}>{error}</div>
        ) : detail !== null ? (
          <ReviewCaseDetailContent detail={detail} loading={loading} error={error} onChanged={handleChanged} defaultSurvivorPersonId={defaultSurvivorPersonId} />
        ) : null}
      </div>
    </div>
  );
}
