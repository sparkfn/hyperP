"use client";

import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

import ActionToast, { type ToastState } from "@/components/ActionToast";
import MergeOverlay from "@/components/MergeOverlay";
import ReviewActionsPanel from "@/components/ReviewActionsPanel";
import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonComparisonEntity, ReviewCaseActionEntry, ReviewCaseDetail } from "@/lib/api-types-ops";
import { completenessColor, formatDate, relativeTime } from "@/lib/display";
import { useSetLoading } from "@/lib/LoadingContext";
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

function reviewSubjectTitle(detail: ReviewCaseDetail): string {
  const leftName = detail.comparison_left?.preferred_full_name ?? null;
  const rightName = detail.comparison_right?.preferred_full_name ?? null;
  if (leftName !== null && rightName !== null) return `${leftName} ↔ ${rightName}`;
  if (leftName !== null) return leftName;
  if (rightName !== null) return rightName;
  return `${titleCase(detail.match_decision.decision)} case`;
}

function reviewSubjectSubtitle(detail: ReviewCaseDetail): string {
  const leftPersonId = detail.comparison_left?.person_id ?? null;
  const rightPersonId = detail.comparison_right?.person_id ?? null;
  if (leftPersonId !== null && rightPersonId !== null) return `Pair audit · ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
  if (leftPersonId !== null || rightPersonId !== null) return `Person review · ${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
  return `${shortCaseId(detail.review_case_id)} · ${titleCase(detail.match_decision.engine_type)} engine`;
}

export default function ReviewCaseDetailPage(): ReactElement {
  const params = useParams<{ reviewCaseId?: string | string[] }>();
  const reviewCaseId = typeof params.reviewCaseId === "string" ? params.reviewCaseId : "";
  const [detail, setDetail] = useState<ReviewCaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const setGlobalLoading = useSetLoading();
  const loadId = useMemo(() => `review-case-${reviewCaseId}`, [reviewCaseId]);

  const loadDetail = useCallback((): Promise<void> => {
    if (reviewCaseId.length === 0) {
      setError("Review case id is missing.");
      setLoading(false);
      return Promise.resolve();
    }
    setLoading(true);
    setError(null);
    setGlobalLoading(loadId, true);
    return bffFetch<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}`)
      .then((res) => setDetail(res))
      .catch((err: unknown) => {
        setError(err instanceof BffError ? err.message : "Failed to load review case.");
      })
      .finally(() => {
        setLoading(false);
        setGlobalLoading(loadId, false);
      });
  }, [loadId, reviewCaseId, setGlobalLoading]);

  useEffect(() => {
    queueMicrotask(loadDetail);
  }, [loadDetail]);

  if (loading && detail === null) {
    return <ReviewDetailSkeleton />;
  }

  if (error !== null && detail === null) {
    return (
      <div className={styles.page}>
        <div className={styles.heading}>
          <Link href="/review" className={styles.backLink}>← Review queue</Link>
        </div>
        <div className={styles.filterSection}><div className={styles.error}>{error}</div></div>
      </div>
    );
  }

  if (detail === null) {
    return (
      <div className={styles.page}>
        <div className={styles.filterSection}><div className={styles.empty}>Review case not found.</div></div>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <div className={styles.detailTopbar}>
        <Link href="/review" className={styles.backLink}>← Review queue</Link>
        {loading ? <span className={styles.muted}>Refreshing…</span> : null}
      </div>
      <ReviewCaseDetailContent detail={detail} loading={loading} error={error} onChanged={loadDetail} />
    </div>
  );
}

function ReviewCaseDetailContent({
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
              <KeyValue label="Outcome" value={detail.match_decision.decision} />
              <KeyValue label="Engine" value={detail.match_decision.engine_type} />
              <KeyValue label="Assigned to" value={detail.assigned_to ?? "—"} />
              <KeyValue label="Follow-up" value={detail.follow_up_at ?? "—"} />
              <KeyValue label="SLA" value={detail.sla_due_at ?? "—"} />
              <KeyValue label="Resolution" value={detail.resolution ?? "—"} />
            </div>
            <div className={styles.reasonBox}>
              <div className={styles.reasonBoxLabel}>Reasons</div>
              {detail.match_decision.reasons.join(" · ") || "No reasons recorded."}
            </div>
          </section>

          <div className={styles.compareGrid}>
            <ComparisonCard title="Left" entity={detail.comparison_left} compact={compact} />
            <ComparisonCard title="Right" entity={detail.comparison_right} compact={compact} />
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
            onChanged={onChanged}
            onActionBusy={onActionBusy}
            onActionDone={onActionDone}
          />
        </aside>
      </div>
    </>
  );
}

function ReviewDetailSkeleton(): ReactElement {
  return (
    <div className={styles.page}>
      <div className={styles.detailTopbar}>
        <span className={styles.skeletonLine} style={{ width: 110 }} />
      </div>

      <section className={styles.detailHero}>
        <div className={styles.heroIdentity}>
          <span className={`${styles.caseAvatar} ${styles.skeletonAvatar}`} />
          <div className={styles.heroCopy}>
            <span className={styles.skeletonLine} style={{ width: 90 }} />
            <span className={styles.skeletonLine} style={{ width: 260, height: 22 }} />
            <span className={styles.skeletonLine} style={{ width: 220 }} />
            <span className={styles.skeletonLine} style={{ width: 180 }} />
          </div>
        </div>
        <div className={styles.heroStats}>
          <span className={`${styles.badge} ${styles.skeletonBadge}`} />
          <span className={`${styles.badge} ${styles.skeletonBadge}`} />
          <span className={`${styles.badge} ${styles.skeletonBadgeWide}`} />
        </div>
      </section>

      <div className={styles.detailBody}>
        <div className={styles.detailMainColumn}>
          <section className={styles.detailCard}>
            <span className={styles.skeletonLine} style={{ width: 80 }} />
            <div className={styles.kvGrid}>
              {Array.from({ length: 6 }, (_, index) => (
                <div key={index} className={styles.kvItem}>
                  <span className={styles.skeletonLine} style={{ width: 74 }} />
                  <span className={styles.skeletonLine} style={{ width: `${70 - index * 4}%` }} />
                </div>
              ))}
            </div>
          </section>

          <div className={styles.compareGrid}>
            {Array.from({ length: 2 }, (_, index) => (
              <section key={index} className={`${styles.detailCard} ${styles.comparisonCard}`}>
                <span className={styles.skeletonLine} style={{ width: 54 }} />
                <div className={styles.comparisonHeader}>
                  <span className={`${styles.caseAvatar} ${styles.skeletonAvatar}`} />
                  <div className={styles.comparisonCopy}>
                    <span className={`${styles.skeletonLine} ${styles.comparisonNameSkeleton}`} />
                    <span className={`${styles.skeletonLine} ${styles.comparisonMetaSkeleton}`} />
                  </div>
                </div>
                <div className={styles.comparisonFields}>
                  {Array.from({ length: 6 }, (_, rowIndex) => (
                    <div key={rowIndex} className={styles.kvItem}>
                      <span className={styles.skeletonLine} style={{ width: 70 }} />
                      <span className={styles.skeletonLine} style={{ width: `${82 - rowIndex * 7}%` }} />
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>

          <section className={styles.detailCard}>
            <span className={styles.skeletonLine} style={{ width: 120 }} />
            <div className={styles.actionList}>
              {Array.from({ length: 3 }, (_, index) => (
                <div key={index} className={styles.actionItem}>
                  <span className={styles.skeletonLine} style={{ width: `${60 - index * 8}%` }} />
                  <span className={styles.skeletonLine} style={{ width: `${80 - index * 10}%` }} />
                </div>
              ))}
            </div>
          </section>
        </div>

        <aside className={styles.reviewActionRail}>
          <section className={styles.detailCard}>
            <span className={styles.skeletonLine} style={{ width: 130 }} />
            <div className={styles.actionSection}>
              <span className={styles.skeletonLine} style={{ width: 86 }} />
              <span className={styles.skeletonLine} style={{ width: "100%", height: 36 }} />
            </div>
            <div className={styles.actionSection}>
              <span className={styles.skeletonLine} style={{ width: 100 }} />
              <div className={styles.mergeCompare}>
                {Array.from({ length: 5 }, (_, index) => (
                  <span key={index} className={styles.skeletonLine} style={{ width: "100%", height: index === 0 ? 32 : 46 }} />
                ))}
              </div>
              <span className={styles.skeletonLine} style={{ width: "100%", height: 78 }} />
              <span className={styles.skeletonLine} style={{ width: 140, height: 36 }} />
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
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

function ComparisonCard({ title, entity, compact = false }: { title: string; entity: PersonComparisonEntity | null; compact?: boolean }): ReactElement {
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

  if (compact) {
    const compactFields: { icon: ReactElement; value: string | null; title: string }[] = [
      { title: "Phone", value: entity.preferred_phone, icon: <svg {...compactSvg}><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.61 3.41 2 2 0 0 1 3.6 1h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.6a16 16 0 0 0 6 6l.96-.96a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" /></svg> },
      { title: "Email", value: entity.preferred_email, icon: <svg {...compactSvg}><rect width="20" height="16" x="2" y="4" rx="2" /><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" /></svg> },
      { title: "DOB", value: entity.preferred_dob, icon: <svg {...compactSvg}><rect width="18" height="18" x="3" y="4" rx="2" /><line x1="16" x2="16" y1="2" y2="6" /><line x1="8" x2="8" y1="2" y2="6" /><line x1="3" x2="21" y1="10" y2="10" /></svg> },
      { title: "Address", value: entity.preferred_address?.normalized_full ?? null, icon: <svg {...compactSvg}><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" /><circle cx="12" cy="10" r="3" /></svg> },
    ];
    return (
      <section className={`${styles.detailCard} ${styles.comparisonCard}`}>
        <div className={styles.comparisonCompactHeader}>
          <span className={styles.comparisonLabel}>{title}</span>
          {entity.status !== null && <span className={styles.comparisonStatus}>{entity.status}</span>}
        </div>
        <div className={styles.comparisonCompactName}>
          <span className={styles.caseAvatar} style={{ background: subjectAvatarColor(displayName), width: 22, height: 22, fontSize: 9, flexBasis: 22 }}>
            {subjectInitials(displayName)}
          </span>
          <span className={styles.comparisonName}>{displayName}</span>
        </div>
        <div className={styles.comparisonCompactFields}>
          {compactFields.map(({ icon, value, title: fieldTitle }) => (
            <div key={fieldTitle} className={styles.comparisonCompactRow} title={fieldTitle}>
              <span className={styles.comparisonCompactIcon}>{icon}</span>
              <span className={styles.comparisonCompactValue}>{value ?? "—"}</span>
            </div>
          ))}
        </div>
        {personHref !== null ? <Link className={styles.profileLinkButton} href={personHref} style={{ fontSize: 11, minHeight: 28, padding: "0 10px" }}>Open profile</Link> : null}
      </section>
    );
  }

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

export function ReviewCaseDetailModal({
  open,
  reviewCaseId,
  onClose,
}: {
  open: boolean;
  reviewCaseId: string;
  onClose: () => void;
}): ReactElement | null {
  const [detail, setDetail] = useState<ReviewCaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [toast, setToast] = useState<ToastState | null>(null);

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
      <div className={styles.reviewCaseModal} onClick={(e) => e.stopPropagation()}>
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
              window.location.reload();
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
