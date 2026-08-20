"use client";

import { useState, useEffect, useId, useRef, useCallback, useMemo, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactElement, type ReactNode } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import type {
  EntityFilterOption,
  ListedPerson,
  PersonConnection,
  PersonListSummary,
  SalesOrder,
} from "@/lib/api-types";
import { bffFetchEnvelope, BffError, bffFetch } from "@/lib/api-client";
import type { SourceSystemInfo } from "@/lib/api-types-ops";
import { avatarColor, completenessColor, getInitials } from "@/lib/display";
import { useSetLoading } from "@/lib/LoadingContext";
import { usePopoverClose } from "@/lib/usePopoverClose";
import { personDisplayName } from "@/lib/ui-display";
import { normalizePaginatedPage } from "@/lib/paginated-page";
import PersonGraphDialog from "@/components/PersonGraphDialog";
import { formatPaginationShowing, hasEffectivePersonListFilters } from "./person-list-view";
import { useLazyFilterOptions } from "./use-lazy-filter-options";
import { CrmDealCount } from "./CrmDealCount";
import {
  PERSON_LIST_COLUMNS as COLS,
  PERSON_LIST_DEFAULT_WIDTHS as DEFAULT_WIDTHS,
  PERSON_LIST_TABLE_COLUMN_COUNT,
} from "./person-list-columns";
import {
  applyCrmDealRange,
  crmDealPreset,
  crmDealRangeLabel,
  parseCrmDealRange,
  type CrmDealRange,
} from "./person-list-crm-controls";
import styles from "./persons.module.css";
const NAME_COL_OVERHEAD = 74; // left-pad(14) + avatar(36) + gap(10) + right-pad(14)
const NAME_COL_MIN = 360;
const NAME_COL_MAX = 500;
const ADDR_COL_OVERHEAD = 28; // left-pad(14) + right-pad(14)
const ADDR_COL_MIN = 96;
const ADDR_COL_MAX = 180;


type RelationPopoverState = {
  personId: string;
  anchorTop: number;
  anchorRight: number;
};

type OrdersPopoverState = {
  personId: string;
  anchorTop: number;
  anchorRight: number;
};

function formatRelationType(connection: PersonConnection): string {
  if (connection.knows_relationships.length > 0) {
    return connection.knows_relationships
      .map((relationship) => relationship.relationship_label ?? relationship.relationship_category)
      .join(" • ");
  }
  if (connection.shared_identifiers.length > 0) {
    const sharedTypes = connection.shared_identifiers.map((identifier) => identifier.identifier_type).join(", ");
    return `Shared identifier: ${sharedTypes}`;
  }
  if (connection.shared_addresses.length > 0) {
    return "Shared address";
  }
  return connection.hops > 1 ? "Graph-linked relation" : "Graph-linked relation";
}


function formatOrderCurrency(order: SalesOrder): string {
  if (order.total_amount == null) {
    return "—";
  }
  const currency = order.currency ?? "SGD";
  return new Intl.NumberFormat("en-SG", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(order.total_amount);
}

function formatOrderDate(date: string | null): string {
  if (!date) {
    return "—";
  }
  const d = date.includes("T") ? new Date(date) : new Date(`${date}T00:00:00`);
  if (Number.isNaN(d.getTime())) {
    return "—";
  }
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: process.env.NEXT_PUBLIC_TZ ?? "UTC",
  }).format(d);
}


function OrdersPopover({
  personId,
  anchorTop,
  anchorRight,
  orders,
  loading,
  total,
  onClose,
}: {
  personId: string;
  anchorTop: number;
  anchorRight: number;
  orders: SalesOrder[];
  loading: boolean;
  total: number | null;
  onClose: () => void;
}): ReactElement {
  const popoverRef = usePopoverClose<HTMLDivElement>(onClose);

  return (
    <div
      ref={popoverRef}
      className={styles.ordersPopover}
      style={{ top: anchorTop, right: anchorRight }}
      role="dialog"
      aria-label={`Orders for ${personId}`}
    >
      <div className={styles.ordersPopoverHeader}>
        <div>
          <div className={styles.ordersPopoverTitle}>Order history</div>
          <div className={styles.ordersPopoverSubtitle}>
            {loading ? "Loading…" : total !== null && total > orders.length ? `Showing ${orders.length} of ${total}` : `${orders.length} order${orders.length === 1 ? "" : "s"}`}
          </div>
        </div>
        <button type="button" className={styles.ordersPopoverClose} onClick={onClose} aria-label="Close orders popover">×</button>
      </div>
      <div className={styles.ordersList}>
        {loading ? (
          <>
            {[0, 1, 2].map((i) => (
              <div key={i} className={styles.skeletonOrderItem}>
                <div className={styles.skeletonOrderItemTop}>
                  <span className={styles.skeletonLine} style={{ width: "55%" }} />
                  <span className={styles.skeletonLine} style={{ width: "22%" }} />
                </div>
                <span className={styles.skeletonLine} style={{ width: "70%", height: "10px" }} />
                <span className={styles.skeletonLine} style={{ width: "45%", height: "9px" }} />
              </div>
            ))}
          </>
        ) : orders.map((order, index) => {
          const primaryProduct = order.line_items[0]?.product?.display_name ?? "Order item";
          const secondaryLine = [order.entity_name, formatOrderDate(order.order_date)].filter(Boolean).join(" • ");
          return (
            <div key={order.order_no ?? `${personId}-order-${index}`} className={styles.orderItem}>
              <div className={styles.orderItemTop}>
                <div className={styles.orderNumber} title={order.order_no ?? order.source_order_id ?? ""}>
                  {order.order_no ?? order.source_order_id ?? "—"}
                </div>
                <div className={styles.orderAmount}>{formatOrderCurrency(order)}</div>
              </div>
              <div className={styles.orderProduct}>{primaryProduct}</div>
              <div className={styles.orderMeta}>{secondaryLine || "—"}</div>
            </div>
          );
        })}
      </div>
      <div className={styles.popoverFooter}>
        <Link href={`/persons/${personId}?tab=sales`} className={styles.popoverViewAll} onClick={onClose}>
          View all
        </Link>
      </div>
    </div>
  );
}

function RelationPopover({
  personId,
  anchorTop,
  anchorRight,
  connections,
  loading,
  total,
  onClose,
}: {
  personId: string;
  anchorTop: number;
  anchorRight: number;
  connections: PersonConnection[];
  loading: boolean;
  total: number | null;
  onClose: () => void;
}): ReactElement {
  const popoverRef = usePopoverClose<HTMLDivElement>(onClose);

  return (
    <div
      ref={popoverRef}
      className={styles.relationsPopover}
      style={{ top: anchorTop, right: anchorRight }}
      role="dialog"
      aria-label={`Relations for ${personId}`}
    >
      <div className={styles.relationsPopoverHeader}>
        <div>
          <div className={styles.relationsPopoverTitle}>Relations</div>
          <div className={styles.relationsPopoverSubtitle}>
            {loading ? "Loading…" : total !== null && total > connections.length ? `Showing ${connections.length} of ${total}` : `${connections.length} linked profile${connections.length === 1 ? "" : "s"}`}
          </div>
        </div>
        <button type="button" className={styles.relationsPopoverClose} onClick={onClose} aria-label="Close relations popover">
          ×
        </button>
      </div>
      <div className={styles.relationsList}>
        {loading ? (
          <>
            {[0, 1, 2].map((i) => (
              <div key={i} className={styles.skeletonRelationItem}>
                <span className={styles.skeletonLine} style={{ width: "65%" }} />
                <span className={styles.skeletonLine} style={{ width: "40%", height: "9px" }} />
                <span className={styles.skeletonLine} style={{ width: "55%", height: "10px" }} />
              </div>
            ))}
          </>
        ) : connections.map((connection) => (
          <div key={connection.person_id} className={styles.relationItem}>
            <div className={styles.relationItemTop}>
              <Link href={`/persons/${connection.person_id}`} className={styles.relationLink} onClick={onClose}>
                {personDisplayName(connection.preferred_full_name)}
              </Link>
            </div>
            <div className={styles.relationSummary}>{formatRelationType(connection)}</div>
          </div>
        ))}
      </div>
      <div className={styles.popoverFooter}>
        <Link href={`/persons/${personId}?tab=connections`} className={styles.popoverViewAll} onClick={onClose}>
          View all
        </Link>
      </div>
    </div>
  );
}

const RING_R = 16;
const RING_CIRC = 2 * Math.PI * RING_R;

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }): ReactElement {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
      <path
        d="M5 7L8 4L11 7"
        stroke={active && dir === "asc" ? "var(--accent)" : "var(--text-faint)"}
        strokeWidth={active && dir === "asc" ? 2 : 1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M5 9L8 12L11 9"
        stroke={active && dir === "desc" ? "var(--accent)" : "var(--text-faint)"}
        strokeWidth={active && dir === "desc" ? 2 : 1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function AvatarRing({ initials, color, score }: { initials: string; color: string; score: number }): ReactElement {
  const ringColor = completenessColor(score);
  const dash = RING_CIRC * score;
  const gap  = RING_CIRC - dash;
  return (
    <div className={styles.avatarWrap}>
      <svg className={styles.avatarRing} viewBox="0 0 36 36" fill="none">
        {/* track */}
        <circle cx="18" cy="18" r={RING_R} stroke={ringColor} strokeWidth="1.5" opacity="0.18" />
        {/* progress */}
        <circle
          cx="18" cy="18" r={RING_R}
          stroke={ringColor} strokeWidth="1.5"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${gap}`}
          transform="rotate(-90 18 18)"
        />
      </svg>
      <div className={styles.avatar} style={{ background: color }}>{initials}</div>
    </div>
  );
}

type LabelDef = { text: string; cls: string };

function getLabels(p: ListedPerson): LabelDef[] {
  const labels: LabelDef[] = [];
  const score = p.profile_completeness_score;
  const daysSince = (Date.now() - new Date(p.created_at).getTime()) / 86_400_000;

  // Mutually exclusive — pick one
  if (p.is_high_risk)      labels.push({ text: "Risk", cls: styles.flagHR  ?? "" });
  else if (p.is_high_value) labels.push({ text: "High", cls: styles.flagHV  ?? "" });
  else if (score < 0.3)    labels.push({ text: "Low",  cls: styles.flagLow ?? "" });
  // New is additive
  if (daysSince < 30)      labels.push({ text: "New",  cls: styles.flagNew ?? "" });
  return labels;
}

function PersonRow({
  p,
  checked,
  onToggle,
  onEntityClick,
  onRelationsClick,
  onOrdersClick,
  onGraphClick,
  isResizing,
}: {
  p: ListedPerson;
  checked: boolean;
  onToggle: () => void;
  onEntityClick: (key: string) => void;
  onRelationsClick: (event: ReactMouseEvent<HTMLButtonElement>, personId: string) => void;
  onOrdersClick: (event: ReactMouseEvent<HTMLButtonElement>, personId: string) => void;
  onGraphClick: (personId: string, name: string) => void;
  isResizing: () => boolean;
}): ReactElement {
  const initials = getInitials(p.preferred_full_name);
  const pct = Math.round(p.profile_completeness_score * 100);
  const labels = getLabels(p);
  const router = useRouter();
  const personHref = `/persons/${p.person_id}`;

  const handleRowClick = (): void => {
    if (isResizing()) return;
    router.push(personHref);
  };

  const handleRowKeyDown = (event: ReactKeyboardEvent<HTMLTableRowElement>): void => {
    if (isResizing()) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      router.push(personHref);
    }
  };

  return (
    <tr
      className={styles.tr}
      onClick={handleRowClick}
      onKeyDown={handleRowKeyDown}
      tabIndex={0}
      role="link"
      aria-label={`Open ${p.preferred_full_name ?? p.person_id} details`}
    >
      <td className={styles.tdCheck} onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" checked={checked} onChange={onToggle} className={styles.checkbox} />
      </td>
      <td className={`${styles.td} ${styles.tdName}`}>
        <Link href={personHref} className={styles.personLink} onClick={(event) => event.stopPropagation()}>
          <div className={styles.personCell}>
            <AvatarRing initials={initials} color={avatarColor(p.preferred_full_name ?? "?")} score={p.profile_completeness_score} />
            <div className={styles.personInfo}>
              <div className={styles.personName}>{personDisplayName(p.preferred_full_name)}</div>
              <div className={styles.personMeta}>
                {labels.map((l) => (
                  <span key={l.text} className={`${styles.flag} ${l.cls}`}>{l.text}</span>
                ))}
              </div>
            </div>
          </div>
        </Link>
      </td>
      <td className={`${styles.td} ${styles.tdTruncate}`} style={{ fontFamily: "monospace", fontSize: 12, color: "var(--text-secondary)" }}>
        <span className={styles.clip} title={p.preferred_nric ?? ""}>
          {p.preferred_nric ?? <span style={{ color: "var(--text-muted)" }}>—</span>}
        </span>
      </td>
      <td className={`${styles.td} ${styles.tdTruncate}`} style={{ color: "var(--text-secondary)", fontSize: 12 }}>
        <span className={styles.clip} title={p.preferred_phone ?? ""}>
          {p.preferred_phone ?? <span style={{ color: "var(--text-muted)" }}>—</span>}
        </span>
      </td>
      <td className={styles.td}>
        <div className={styles.dobCell}>
          <span className={p.preferred_dob_invalid ? styles.dobBad : styles.dobText}>{p.preferred_dob_display}</span>
          {p.preferred_dob_invalid && (
            <span className={styles.dobWarn} title="Invalid or placeholder date of birth">⚠️</span>
          )}
        </div>
      </td>
      <td className={`${styles.td} ${styles.tdTruncate}`} style={{ color: "var(--text-secondary)", fontSize: 12 }}>
        <span className={styles.clip} title={p.preferred_email ?? ""}>
          {p.preferred_email ?? <span style={{ color: "var(--text-muted)" }}>—</span>}
        </span>
      </td>
      <td className={`${styles.td} ${styles.tdTruncate}`} style={{ fontSize: 12, color: "var(--text-secondary)" }}>
        <span className={styles.clip} title={p.preferred_address?.normalized_full ?? ""}>
          {p.preferred_address?.normalized_full ?? <span style={{ color: "var(--text-muted)" }}>—</span>}
        </span>
      </td>
      <td className={styles.td}>
        <div className={styles.entityTags}>
          {p.entities.map((e) => (
            <button
              key={e.entity_key}
              className={styles.entityTag}
              onClick={(ev) => { ev.stopPropagation(); onEntityClick(e.entity_key); }}
              title="Filter by this entity"
            >
              {e.display_name ?? e.entity_key}
            </button>
          ))}
          {p.entities.length === 0 && <span style={{ color: "var(--text-muted)" }}>—</span>}
        </div>
      </td>
      <td className={`${styles.td} ${styles.tdMetric}`}>
        {p.connection_count > 0 ? (
          <button
            type="button"
            className={styles.relationsTrigger}
            onClick={(event) => {
              event.stopPropagation();
              onRelationsClick(event, p.person_id);
            }}
            aria-haspopup="dialog"
            aria-label={`Show ${p.connection_count} relations for ${p.preferred_full_name ?? p.person_id}`}
          >
            <span className={styles.relationsCount}>{p.connection_count}</span>
            <span className={styles.relationsLabel}>linked</span>
          </button>
        ) : (
          <span className={styles.relationsZero}>No links</span>
        )}
      </td>
      <td className={`${styles.td} ${styles.tdMetric}`}>
        {p.order_count > 0 ? (
          <button
            type="button"
            className={styles.ordersTrigger}
            onClick={(event) => {
              event.stopPropagation();
              onOrdersClick(event, p.person_id);
            }}
            aria-haspopup="dialog"
            aria-label={`Show ${p.order_count} orders for ${p.preferred_full_name ?? p.person_id}`}
          >
            <span className={styles.ordersCount}>{p.order_count.toLocaleString()}</span>
            <span className={styles.ordersLabel}>orders</span>
          </button>
        ) : (
          <span className={styles.ordersZero}>No orders</span>
        )}
      </td>
      <td className={`${styles.td} ${styles.tdMetric}`}>
        <CrmDealCount count={p.crm_deal_count} />
      </td>
      <td className={`${styles.td} ${styles.tdMetric}`}>
        {p.system_match_count > 0 ? (
          <Link
            href={`/persons/${p.person_id}?tab=matches`}
            className={styles.matchesTrigger}
            onClick={(event) => event.stopPropagation()}
            aria-label={`Open ${p.system_match_count} recommended matches for ${p.preferred_full_name ?? p.person_id}`}
          >
            <span className={styles.matchesCount}>{p.system_match_count}</span>
            <span className={styles.matchesLabel}>recommended</span>
          </Link>
        ) : (
          <span className={styles.matchesZero}>No recommended</span>
        )}
      </td>
      <td className={`${styles.td} ${styles.tdMetric}`}>
        {p.system_match_count > 0 ? (
          <Link
            href={`/persons/${p.person_id}#section-decision-history`}
            className={styles.matchesTrigger}
            onClick={(event) => event.stopPropagation()}
            aria-label={`Open ${p.system_match_count} decision history entries for ${p.preferred_full_name ?? p.person_id}`}
          >
            <span className={styles.matchesCount}>{p.system_match_count}</span>
            <span className={styles.matchesLabel}>decisions</span>
          </Link>
        ) : (
          <span className={styles.matchesZero}>No decisions</span>
        )}
      </td>
      <td className={`${styles.td} ${styles.td}`}>
        <div className={styles.qualityWrap}>
          <div className={styles.qualityBar}>
            <div
              className={styles.qualityFill}
              style={{ width: `${pct}%`, background: completenessColor(p.profile_completeness_score) }}
            />
          </div>
          <span className={styles.qualityText}>{pct}%</span>
        </div>
      </td>
      <td className={`${styles.tdGraph} ${styles.tdSticky} ${styles.stickyGraph}`}>
        <button
          type="button"
          className={styles.graphLink}
          title="View in graph"
          onClick={(event) => {
            event.stopPropagation();
            onGraphClick(p.person_id, p.preferred_full_name ?? p.person_id);
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
          </svg>
        </button>
      </td>
    </tr>
  );
}

function PersonCardMobile({
  p,
  onGraphClick,
}: {
  p: ListedPerson;
  onGraphClick: (personId: string, name: string) => void;
}): ReactElement {
  const router = useRouter();
  const initials = getInitials(p.preferred_full_name);
  const pct = Math.round(p.profile_completeness_score * 100);
  const labels = getLabels(p);

  return (
    <div
      role="button"
      tabIndex={0}
      className={styles.mobileCard}
      onClick={() => router.push(`/persons/${p.person_id}`)}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") router.push(`/persons/${p.person_id}`); }}
      aria-label={`Open ${p.preferred_full_name ?? p.person_id}`}
    >
      <AvatarRing initials={initials} color={avatarColor(p.preferred_full_name ?? "?")} score={p.profile_completeness_score} />

      <div className={styles.mobileCardBody}>
        <div className={styles.mobileCardTop}>
          <span className={styles.mobileCardName}>{personDisplayName(p.preferred_full_name)}</span>
          <div className={styles.mobileCardFlags}>
            {labels.map((l) => (
              <span key={l.text} className={`${styles.flag} ${l.cls}`}>{l.text}</span>
            ))}
          </div>
        </div>
        <div className={styles.mobileCardSub}>
          {p.preferred_phone && <span className={styles.mobileCardInfo}>{p.preferred_phone}</span>}
          {!p.preferred_phone && p.preferred_email && <span className={styles.mobileCardInfo}>{p.preferred_email}</span>}
        </div>
      </div>

      <div className={styles.mobileCardRight}>
        <div className={styles.mobileCardQuality}>
          <div className={styles.mobileCardQualityBar}>
            <div className={styles.mobileCardQualityFill} style={{ width: `${pct}%`, background: completenessColor(p.profile_completeness_score) }} />
          </div>
          <span className={styles.mobileCardQualityPct}>{pct}%</span>
        </div>
        {(p.connection_count > 0 || p.order_count > 0 || p.crm_deal_count > 0) && (
          <div className={styles.mobileCardCounts}>
            {p.connection_count > 0 && <span className={styles.mobileCardCount}>{p.connection_count} linked</span>}
            {p.order_count > 0 && <span className={styles.mobileCardCount}>{p.order_count} orders</span>}
            {p.crm_deal_count > 0 && <span className={styles.mobileCardCount}>{p.crm_deal_count} deals</span>}
          </div>
        )}
      </div>

      <button
        type="button"
        className={styles.mobileCardGraph}
        title="View in graph"
        onClick={(e) => { e.stopPropagation(); onGraphClick(p.person_id, p.preferred_full_name ?? p.person_id); }}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
          <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
        </svg>
      </button>
    </div>
  );
}

const SKELETON_WIDTHS: readonly number[][] = [
  [55, 90, 70, 80, 75, 110, 140, 60, 40, 36, 40, 80],
  [70, 80, 60, 90, 85, 130, 100, 80, 50, 44, 50, 65],
  [60, 95, 75, 70, 90, 120, 160, 70, 42, 40, 42, 72],
  [50, 85, 65, 85, 80, 100, 130, 55, 38, 48, 38, 58],
  [65, 100, 80, 75, 70, 140, 110, 75, 46, 36, 46, 68],
  [72, 78, 68, 88, 95, 115, 150, 65, 44, 52, 44, 76],
  [58, 92, 72, 78, 78, 125, 120, 68, 40, 40, 40, 62],
  [68, 86, 58, 82, 82, 135, 145, 72, 48, 44, 48, 70],
] as const;

const FALLBACK_WIDTHS: readonly number[] = [55, 90, 70, 80, 75, 110, 140, 60, 40, 36, 40, 80] as const;

function SkeletonRow({ index, colWidths }: { index: number; colWidths: number[] }): ReactElement {
  const row = SKELETON_WIDTHS[index % SKELETON_WIDTHS.length] ?? FALLBACK_WIDTHS;
  const w = (i: number): number => row[i] ?? FALLBACK_WIDTHS[i] ?? 60;
  return (
    <tr className={`${styles.tr} ${styles.skeletonRow}`}>
      <td className={styles.tdCheck}>
        <span className={styles.skeleton} style={{ width: 14, height: 14, borderRadius: 3, display: "block" }} />
      </td>
      {/* name cell — avatar + two lines */}
      <td className={`${styles.td} ${styles.tdName}`} style={{ minWidth: colWidths[1] ?? 180 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className={styles.skeletonAvatar} />
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <span className={styles.skeletonLine} style={{ width: w(0) }} />
            <span className={styles.skeletonLine} style={{ width: Math.round(w(0) * 0.6), height: 10 }} />
          </div>
        </div>
      </td>
      {/* All non-name, non-sticky data cells through decision history. */}
      {COLS.slice(2, -2).map((column, index) => (
        <td key={column.key} className={styles.td}>
          <span className={styles.skeletonLine} style={{ width: w(index + 1) }} />
        </td>
      ))}
      {/* completeness — bar + number */}
      <td className={`${styles.td} ${styles.td}`}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span className={styles.skeleton} style={{ flex: 1, height: 6, borderRadius: 3 }} />
          <span className={styles.skeletonLine} style={{ width: w(9), height: 11 }} />
        </div>
      </td>
      {/* graph icon */}
      <td className={`${styles.tdGraph} ${styles.tdSticky} ${styles.stickyGraph}`}>
        <span className={styles.skeleton} style={{ width: 18, height: 18, borderRadius: 4, display: "block", margin: "0 auto" }} />
      </td>
    </tr>
  );
}

function PaginationBar({
  showing, hasPrev, hasNext, pageSize, selectedCount, onPrev, onNext, onPageSize, bottom,
}: {
  showing: string; hasPrev: boolean; hasNext: boolean; pageSize: number; selectedCount: number;
  onPrev: () => void; onNext: () => void; onPageSize: (n: number) => void; bottom?: boolean;
}): ReactElement {
  return (
    <div className={bottom ? styles.paginationBottom : styles.countBar}>
      <span className={styles.resultCount}>{showing}</span>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: "auto" }}>
        {selectedCount > 0 && <span className={styles.selectionBadge}>{selectedCount} selected</span>}
        <select className={styles.pageSizeSelect} value={pageSize} onChange={(e) => onPageSize(Number(e.target.value))}>
          {[25, 50, 100, 200].map((n) => <option key={n} value={n}>{n} / page</option>)}
        </select>
        {(hasPrev || hasNext) && (
          <>
            <button className={styles.pageBtn} disabled={!hasPrev} onClick={onPrev}>← Prev</button>
            <button className={styles.pageBtn} disabled={!hasNext} onClick={onNext}>Next →</button>
          </>
        )}
      </div>
    </div>
  );
}

type PresenceFilter = "any" | "has" | "none";
type DobMode = "single" | "range" | "parts";

const MONTH_OPTIONS: readonly { value: string; label: string }[] = [
  { value: "01", label: "January" },
  { value: "02", label: "February" },
  { value: "03", label: "March" },
  { value: "04", label: "April" },
  { value: "05", label: "May" },
  { value: "06", label: "June" },
  { value: "07", label: "July" },
  { value: "08", label: "August" },
  { value: "09", label: "September" },
  { value: "10", label: "October" },
  { value: "11", label: "November" },
  { value: "12", label: "December" },
] as const;

// 01–31, zero-padded to match the substring comparison against "YYYY-MM-DD".
const DAY_OPTIONS: readonly string[] = Array.from({ length: 31 }, (_, i) =>
  String(i + 1).padStart(2, "0"),
);

function parseDobMode(value: string | null): DobMode {
  return value === "range" || value === "parts" ? value : "single";
}

type SortKey = "name" | "dob" | "entity" | "relations" | "matches" | "decisionHistory" | "orders" | "deals" | "quality";
type SortDir = "asc" | "desc";
type FlagFilter = "any" | "high_value" | "high_risk" | "no_contact";
type FilterKey = "entity" | "source" | "identity" | "matches" | "crmDeals" | "dob" | "address" | "flags" | "bankruptcy" | "updated";

type MatchPresenceFilter = "any" | "has" | "none";

const SORT_PARAM_BY_KEY: Record<SortKey, string> = {
  name: "preferred_full_name",
  dob: "preferred_dob",
  entity: "entity_count",
  relations: "connection_count",
  matches: "system_match_count",
  decisionHistory: "system_match_count",
  orders: "order_count",
  deals: "crm_deal_count",
  quality: "profile_completeness_score",
};

const SORT_KEY_BY_PARAM: Record<string, SortKey> = Object.fromEntries(
  Object.entries(SORT_PARAM_BY_KEY).map(([key, value]) => [value, key]),
) as Record<string, SortKey>;

function parsePresenceFilter(value: string | null): PresenceFilter {
  if (value === "true") return "has";
  if (value === "false") return "none";
  return "any";
}

function parseSortKey(value: string | null): SortKey | null {
  return value ? (SORT_KEY_BY_PARAM[value] ?? null) : null;
}

function parseSortDir(value: string | null): SortDir {
  return value === "asc" ? "asc" : "desc";
}

function parsePageSize(value: string | null): number {
  const parsed = Number(value);
  return [25, 50, 100, 200].includes(parsed) ? parsed : 25;
}

function parseFlagFilter(params: URLSearchParams): FlagFilter {
  if (params.get("is_high_value") === "true") return "high_value";
  if (params.get("is_high_risk") === "true") return "high_risk";
  if (params.get("has_phone") === "false" && params.get("has_email") === "false") return "no_contact";
  return "any";
}

function FilterPill({
  label,
  isActive,
  activeLabel,
  count,
  onClear,
  open,
  onToggle,
  alignRight,
  wide,
  children,
}: {
  label: string;
  isActive: boolean;
  activeLabel: string;
  count?: number;
  onClear: () => void;
  open: boolean;
  onToggle: () => void;
  alignRight?: boolean;
  wide?: boolean;
  children: ReactNode;
}): ReactElement {
  return (
    <div className={styles.fpWrap}>
      <button
        type="button"
        className={`${styles.filterPill} ${isActive ? styles.filterPillActive : ""}`}
        onClick={onToggle}
        aria-expanded={open}
        aria-haspopup="true"
      >
        <span className={styles.fpLabel}>
          {isActive ? activeLabel : label}
          {isActive && count != null && count > 1 && (
            <span className={styles.fpCount}>{count}</span>
          )}
        </span>
        {isActive ? (
          <span
            role="button"
            className={styles.fpClear}
            tabIndex={0}
            aria-label={`Clear ${label} filter`}
            onClick={(e) => { e.stopPropagation(); onClear(); }}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.stopPropagation(); onClear(); } }}
          >×</span>
        ) : (
          <svg width="9" height="9" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M2.5 4.5L6 8L9.5 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        )}
      </button>
      {open && (
        <div className={`${styles.filterPopover} ${alignRight ? styles.filterPopoverRight : ""} ${wide ? styles.filterPopoverWide : ""}`}>
          {children}
        </div>
      )}
    </div>
  );
}

const IDENTITY_FILTERS = [
  { key: "email", label: "Email" },
  { key: "phone", label: "Phone" },
] as const;


function PersonsInner(): ReactElement {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [search, setSearch] = useState(searchParams.get("q") ?? "");
  const [entityFilter, setEntityFilter] = useState<string[]>(() => {
    const values = searchParams.getAll("entity_key");
    const legacy = searchParams.get("entity");
    return values.length > 0 || legacy === null ? values : [legacy];
  });
  const [entityFilterMode, setEntityFilterMode] = useState<"or" | "and">(() => searchParams.get("entity_key_mode") === "and" ? "and" : "or");
  const [sourceFilter, setSourceFilter] = useState<string[]>(() => searchParams.getAll("source_key"));
  const [sourceFilterMode, setSourceFilterMode] = useState<"or" | "and">(() => searchParams.get("source_key_mode") === "and" ? "and" : "or");
  const [hasConversationSource, setHasConversationSource] = useState(() => searchParams.get("source_record_type") === "conversation");
  const [sourceSearch, setSourceSearch] = useState("");
  const [identityFilter, setIdentityFilter] = useState<string[]>(() => {
    const values: string[] = [];
    if (searchParams.get("has_email") === "true") values.push("email");
    if (searchParams.get("has_phone") === "true") values.push("phone");
    return values;
  });
  const [identityFilterMode, setIdentityFilterMode] = useState<"or" | "and">(() => searchParams.get("identity_mode") === "and" ? "and" : "or");
  const [addressFilter, setAddressFilter] = useState<PresenceFilter>(() => parsePresenceFilter(searchParams.get("has_address")));
  const [dobFilter, setDobFilter] = useState<PresenceFilter>(() => parsePresenceFilter(searchParams.get("has_dob")));
  const [dobMode, setDobMode] = useState<DobMode>(() => parseDobMode(searchParams.get("dob_mode")));
  const [dobSingleDate, setDobSingleDate] = useState(searchParams.get("dob") ?? "");
  const [dobStartDate, setDobStartDate] = useState(searchParams.get("dob_from") ?? "");
  const [dobEndDate, setDobEndDate] = useState(searchParams.get("dob_to") ?? "");
  const [dobYear, setDobYear] = useState(searchParams.get("dob_year") ?? "");
  const [dobMonth, setDobMonth] = useState(searchParams.get("dob_month") ?? "");
  const [dobDay, setDobDay] = useState(searchParams.get("dob_day") ?? "");
  const [flagFilter, setFlagFilter] = useState<FlagFilter>(() => parseFlagFilter(searchParams));
  const [hasBankruptcy, setHasBankruptcy] = useState<boolean | null>(() => {
    const value = searchParams.get("has_bankruptcy_case");
    if (value === "true") return true;
    if (value === "false") return false;
    return null;
  });
  const [updatedAfter, setUpdatedAfter] = useState(searchParams.get("updated_after") ?? "");
  const [updatedBefore, setUpdatedBefore] = useState(searchParams.get("updated_before") ?? "");
  const [addrCity, setAddrCity] = useState(searchParams.get("addr_city") ?? "");
  const [addrPostal, setAddrPostal] = useState(searchParams.get("addr_postal") ?? "");
  const [addrCountry, setAddrCountry] = useState(searchParams.get("addr_country") ?? "");
  const [matchPresence, setMatchPresence] = useState<MatchPresenceFilter>(() => {
    const value = searchParams.get("matches");
    return value === "has" || value === "has_any" || value === "possible" ? "has" : value === "none" ? "none" : "any";
  });
  const [crmDealRange, setCrmDealRange] = useState<CrmDealRange>(() =>
    parseCrmDealRange(searchParams),
  );

  const [openFilter, setOpenFilter] = useState<FilterKey | null>(null);
  const [sortKey, setSortKey] = useState<SortKey | null>(() => parseSortKey(searchParams.get("sort_by")));
  const [sortDir, setSortDir] = useState<SortDir>(() => parseSortDir(searchParams.get("sort_order")));
  const [apiRows, setApiRows] = useState<ListedPerson[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [fetchLoading, setFetchLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([]);
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [allProfilesCount, setAllProfilesCount] = useState<number | null>(null);
  const [highRiskCount, setHighRiskCount] = useState<number | null>(null);
  const [highValueCount, setHighValueCount] = useState<number | null>(null);
  const [noContactCount, setNoContactCount] = useState<number | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const [initialListSettled, setInitialListSettled] = useState(false);

  const [graphDialog, setGraphDialog] = useState<{ open: boolean; personId: string; title: string }>({
    open: false,
    personId: "",
    title: "",
  });


  function toggleSort(key: SortKey): void {
    const def: SortDir = key === "name" || key === "dob" ? "asc" : "desc";
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir(def);
    } else {
      const next: SortDir = sortDir === "asc" ? "desc" : "asc";
      if (next === def) {
        setSortKey(null); // third click → reset
      } else {
        setSortDir(next);
      }
    }
  }

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pageSize, setPageSize] = useState(() => parsePageSize(searchParams.get("limit")));
  const listLoadId = useId();
  const setGlobalLoading = useSetLoading();
  useEffect(() => {
    if (searchParams.get("limit") === null && window.innerWidth <= 768) setPageSize(25);
  }, [searchParams]);
  const [canScrollRight, setCanScrollRight] = useState(false);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [colWidths, setColWidths] = useState<number[]>(() => [...DEFAULT_WIDTHS]);
  const [relationPopover, setRelationPopover] = useState<RelationPopoverState | null>(null);
  const [ordersPopover, setOrdersPopover] = useState<OrdersPopoverState | null>(null);
  const [popoverConnections, setPopoverConnections] = useState<PersonConnection[]>([]);
  const [popoverConnectionsLoading, setPopoverConnectionsLoading] = useState(false);
  const [popoverConnectionsTotal, setPopoverConnectionsTotal] = useState<number | null>(null);
  const [popoverOrders, setPopoverOrders] = useState<SalesOrder[]>([]);
  const [popoverOrdersLoading, setPopoverOrdersLoading] = useState(false);
  const [popoverOrdersTotal, setPopoverOrdersTotal] = useState<number | null>(null);
  const checkAllRef = useRef<HTMLInputElement | null>(null);
  const filterBarRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const colEls = useRef<(HTMLTableColElement | null)[]>([]);
  const dragRef = useRef<{ idx: number; startX: number; startW: number } | null>(null);
  const suppressRowClickRef = useRef(false);
  const initialListSettledRef = useRef(false);

  function startColResize(idx: number, e: React.MouseEvent): void {
    e.preventDefault();
    const startW = colWidths[idx] ?? DEFAULT_WIDTHS[idx] ?? 100;
    dragRef.current = { idx, startX: e.clientX, startW };
    suppressRowClickRef.current = true;

    function onMove(ev: MouseEvent): void {
      const d = dragRef.current;
      if (!d) return;
      const min = COLS[d.idx]?.minWidth ?? 50;
      const w = Math.max(min, d.startW + ev.clientX - d.startX);
      const el = colEls.current[d.idx];
      if (el) el.style.width = `${w}px`;
    }

    function onUp(ev: MouseEvent): void {
      const d = dragRef.current;
      if (d) {
        const min = COLS[d.idx]?.minWidth ?? 50;
        const w = Math.max(min, d.startW + ev.clientX - d.startX);
        setColWidths(prev => prev.map((c, i) => i === d.idx ? w : c));
      }
      dragRef.current = null;
      window.setTimeout(() => {
        suppressRowClickRef.current = false;
      }, 0);
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  function handleTableMouseDown(e: React.MouseEvent<HTMLTableElement>): void {
    const target = e.target as HTMLElement;
    if (target.closest(`.${styles.resizeHandle ?? ""}`)) return; // th handles already handle this
    const cell = target.closest("td") as HTMLTableCellElement | null;
    if (!cell) return;
    const rect = cell.getBoundingClientRect();
    if (rect.right - e.clientX > 8) return;
    const colIdx = cell.cellIndex;
    if (!COLS[colIdx]?.resizable) return;
    startColResize(colIdx, e);
  }

  function handleTableMouseMove(e: React.MouseEvent<HTMLTableElement>): void {
    const target = e.target as HTMLElement;
    if (target.closest(`.${styles.resizeHandle ?? ""}`)) return;
    const cell = target.closest("td") as HTMLTableCellElement | null;
    const table = e.currentTarget;
    if (!cell) { table.style.cursor = ""; return; }
    const rect = cell.getBoundingClientRect();
    const nearEdge = rect.right - e.clientX <= 8;
    const resizable = COLS[cell.cellIndex]?.resizable ?? false;
    table.style.cursor = nearEdge && resizable ? "col-resize" : "";
  }

  const checkScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setCanScrollRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 2);
    setCanScrollLeft(el.scrollLeft > 2);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    checkScroll();
    el.addEventListener("scroll", checkScroll, { passive: true });
    const ro = new ResizeObserver(checkScroll);
    ro.observe(el);
    return () => { el.removeEventListener("scroll", checkScroll); ro.disconnect(); };
  }, [checkScroll]);

  useEffect(() => {
    const q = searchParams.get("q");
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (q) setSearch(q);
  }, [searchParams]);

  const shouldLoadEntityOptions = openFilter === "entity" || entityFilter.length > 0;
  const shouldLoadSourceSystems = openFilter === "source" || sourceFilter.length > 0;
  const entityOptions = useLazyFilterOptions<EntityFilterOption>({
    enabled: shouldLoadEntityOptions,
    initialEnabled: entityFilter.length > 0,
    path: "/bff/entities/filter-options",
  });
  const sourceOptions = useLazyFilterOptions<SourceSystemInfo>({
    enabled: shouldLoadSourceSystems,
    initialEnabled: sourceFilter.length > 0,
    path: "/bff/source-systems",
  });
  const entities = entityOptions.items;
  const sourceSystems = sourceOptions.items;

  useEffect(() => {
    if (!initialListSettled) return;
    const controller = new AbortController();
    void bffFetch<PersonListSummary>("/bff/persons/summary", {
      cache: "no-store",
      signal: controller.signal,
    })
      .then((summary) => {
        if (controller.signal.aborted) return;
        setAllProfilesCount(summary.all_profiles_count);
        setHighRiskCount(summary.high_risk_count);
        setHighValueCount(summary.high_value_count);
        setNoContactCount(summary.no_contact_count);
      })
      .catch(() => undefined)
      .finally(() => { if (!controller.signal.aborted) setStatsLoading(false); });
    return () => controller.abort();
  }, [initialListSettled]);

  useEffect(() => {
    function handleOutsideClick(e: MouseEvent): void {
      if (filterBarRef.current && !filterBarRef.current.contains(e.target as Node)) {
        setOpenFilter(null);
      }
    }
    document.addEventListener("mousedown", handleOutsideClick);
    return () => document.removeEventListener("mousedown", handleOutsideClick);
  }, []);

  function toggleFilter(key: FilterKey): void {
    const next = openFilter === key ? null : key;
    if (next === "entity") entityOptions.start();
    if (next === "source") sourceOptions.start();
    setOpenFilter(next);
  }

  const activeFilterCount = [
    dobFilter !== "any",
    dobFilter === "has" && dobMode === "single" && dobSingleDate !== "",
    dobFilter === "has" && dobMode === "range" && (dobStartDate !== "" || dobEndDate !== ""),
    dobFilter === "has" && dobMode === "parts" && (dobYear !== "" || dobMonth !== "" || dobDay !== ""),
    hasConversationSource,
    addressFilter !== "any",
    flagFilter !== "any",
    hasBankruptcy !== null,
    updatedAfter !== "",
    updatedBefore !== "",
    addrCity !== "",
    addrPostal !== "",
    addrCountry !== "",
    matchPresence !== "any",
    crmDealRange.min !== "" || crmDealRange.max !== "",
  ].filter(Boolean).length + entityFilter.length + sourceFilter.length + identityFilter.length;

  function clearAllFilters(): void {
    setEntityFilter([]);
    setEntityFilterMode("or");
    setSourceFilter([]);
    setSourceFilterMode("or");
    setHasConversationSource(false);
    setSourceSearch("");
    setIdentityFilter([]);
    setIdentityFilterMode("or");
    setAddressFilter("any");
    setDobFilter("any");
    setDobMode("single");
    setDobSingleDate("");
    setDobStartDate("");
    setDobEndDate("");
    setDobYear("");
    setDobMonth("");
    setDobDay("");
    setFlagFilter("any");
    setHasBankruptcy(null);
    setUpdatedAfter("");
    setUpdatedBefore("");
    setAddrCity("");
    setAddrPostal("");
    setAddrCountry("");
    setMatchPresence("any");
    setCrmDealRange({ min: "", max: "" });
  }

  function clearDobFilter(): void {
    setDobFilter("any");
    setDobMode("single");
    setDobSingleDate("");
    setDobStartDate("");
    setDobEndDate("");
    setDobYear("");
    setDobMonth("");
    setDobDay("");
  }

  function selectDobMode(mode: DobMode): void {
    setDobMode(mode);
    if (mode !== "single") setDobSingleDate("");
    if (mode !== "range") { setDobStartDate(""); setDobEndDate(""); }
    if (mode !== "parts") { setDobYear(""); setDobMonth(""); setDobDay(""); }
  }

  function dobActiveLabel(): string {
    if (dobFilter === "none") return "No DOB";
    if (dobMode === "single" && dobSingleDate) return `DOB: ${dobSingleDate}`;
    if (dobMode === "range" && (dobStartDate || dobEndDate)) {
      return `DOB: ${dobStartDate || "…"} → ${dobEndDate || "…"}`;
    }
    if (dobMode === "parts") {
      const monthLabel = MONTH_OPTIONS.find((m) => m.value === dobMonth)?.label;
      const pieces: string[] = [];
      if (monthLabel && dobDay) pieces.push(`${monthLabel} ${Number(dobDay)}`);
      else if (monthLabel) pieces.push(monthLabel);
      else if (dobDay) pieces.push(`Day ${Number(dobDay)}`);
      if (/^\d{4}$/.test(dobYear)) pieces.push(dobYear);
      if (pieces.length > 0) return `DOB: ${pieces.join(" ")}`;
    }
    return "Has DOB";
  }

  const apiQuery = useMemo(() => {
    const params = new URLSearchParams();
    if (search.trim().length >= 3) params.set("q", search.trim());
    entityFilter.forEach((key) => params.append("entity_key", key));
    if (entityFilter.length > 1 && entityFilterMode === "and") params.set("entity_key_mode", "and");
    sourceFilter.forEach((key) => params.append("source_key", key));
    if (sourceFilter.length > 1 && sourceFilterMode === "and") params.set("source_key_mode", "and");
    if (hasConversationSource) params.set("source_record_type", "conversation");
    const hasEmail = identityFilter.includes("email");
    const hasPhone = identityFilter.includes("phone");
    if (hasEmail && hasPhone && identityFilterMode === "or") {
      params.set("has_any_contact", "true");
    } else {
      if (hasEmail) params.set("has_email", "true");
      if (hasPhone) params.set("has_phone", "true");
    }
    if (addressFilter === "has") params.set("has_address", "true");
    else if (addressFilter === "none") params.set("has_address", "false");
    if (dobFilter === "has") {
      params.set("has_dob", "true");
      if (dobMode === "single" && dobSingleDate) {
        params.set("dob_from", dobSingleDate);
        params.set("dob_to", dobSingleDate);
      } else if (dobMode === "range") {
        if (dobStartDate) params.set("dob_from", dobStartDate);
        if (dobEndDate) params.set("dob_to", dobEndDate);
      } else if (dobMode === "parts") {
        if (/^\d{4}$/.test(dobYear)) params.set("dob_year", dobYear);
        if (dobMonth) params.set("dob_month", dobMonth);
        if (dobDay) params.set("dob_day", dobDay);
      }
    } else if (dobFilter === "none") {
      params.set("has_dob", "false");
    }
    applyCrmDealRange(params, crmDealRange);
    if (sortKey) {
      params.set("sort_by", SORT_PARAM_BY_KEY[sortKey]);
      params.set("sort_order", sortDir);
    }
    if (flagFilter === "high_value") params.set("is_high_value", "true");
    if (flagFilter === "high_risk") params.set("is_high_risk", "true");
    if (flagFilter === "no_contact") { params.set("has_phone", "false"); params.set("has_email", "false"); }
    if (hasBankruptcy !== null) params.set("has_bankruptcy_case", String(hasBankruptcy));
    if (matchPresence === "has") {
      params.set("has_system_match", "true");
    } else if (matchPresence === "none") {
      params.set("has_system_match", "false");
    }
    if (updatedAfter) params.set("updated_after", updatedAfter);
    if (updatedBefore) params.set("updated_before", updatedBefore);
    if (addrCity) params.set("addr_city", addrCity);
    if (addrPostal) params.set("addr_postal", addrPostal);
    if (addrCountry) params.set("addr_country", addrCountry);
    params.set("limit", String(pageSize));
    return params.toString();
  }, [search, entityFilter, entityFilterMode, sourceFilter, sourceFilterMode, hasConversationSource, identityFilter, identityFilterMode, addressFilter, dobFilter, dobMode, dobSingleDate, dobStartDate, dobEndDate, dobYear, dobMonth, dobDay, flagFilter, hasBankruptcy, matchPresence, crmDealRange, updatedAfter, updatedBefore, addrCity, addrPostal, addrCountry, sortKey, sortDir, pageSize]);

  const urlQuery = useMemo(() => {
    const params = new URLSearchParams(apiQuery);
    if (search.trim()) params.set("q", search.trim());
    else params.delete("q");
    if (dobFilter === "has") params.set("dob_mode", dobMode);
    else params.delete("dob_mode");
    if (matchPresence !== "any") {
      params.set("matches", matchPresence);
      params.delete("match_type");
    } else {
      params.delete("matches");
      params.delete("match_type");
    }
    return params.toString();
  }, [apiQuery, dobFilter, dobMode, matchPresence, search]);

  const hasEffectiveFilters = useMemo(
    () => hasEffectivePersonListFilters(apiQuery),
    [apiQuery],
  );

  const paginationShowing = formatPaginationShowing({
    rowCount: apiRows.length,
    pageIndex: cursorStack.length,
    pageSize,
    exactTotal: total,
    unfilteredSummaryTotal: allProfilesCount,
    hasEffectiveFilters,
  });

  useEffect(() => {
    const current = searchParams.toString();
    if (current === urlQuery) return;
    router.replace(urlQuery ? `/persons?${urlQuery}` : "/persons", { scroll: false });
  }, [router, searchParams, urlQuery]);

  useEffect(() => {
    setCurrentCursor(null);
    setCursorStack([]);
  }, [apiQuery]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const params = new URLSearchParams(apiQuery);
    if (currentCursor) params.set("cursor", currentCursor);
    const requestPath = `/bff/persons?${params.toString()}`;
    // Do not leave rows or pagination from the previous request visible while
    // the fresh result is loading.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setApiRows([]);
    setTotal(null);
    setNextCursor(null);
    setFetchLoading(true);
    setGlobalLoading(listLoadId, true);
    setFetchError(null);
    const run = async (): Promise<void> => {
      try {
        const envelope = await bffFetchEnvelope<ListedPerson[]>(requestPath, {
          cache: "no-store",
          signal,
        });
        if (!signal.aborted) {
          const page = normalizePaginatedPage(envelope);
          setApiRows(page.rows);
          setTotal(page.total);
          setNextCursor(page.nextCursor);
        }
      } catch (err: unknown) {
        if (!signal.aborted) {
          setFetchError(err instanceof BffError ? err.message : "Failed to load data.");
          setApiRows([]);
        }
      } finally {
        if (!signal.aborted) {
          setFetchLoading(false);
          setGlobalLoading(listLoadId, false);
          if (!initialListSettledRef.current) {
            initialListSettledRef.current = true;
            setInitialListSettled(true);
          }
        }
      }
    };
    void run();
    return () => { controller.abort(); setGlobalLoading(listLoadId, false); };
  }, [apiQuery, currentCursor, listLoadId, setGlobalLoading]);

  function goNext(): void {
    if (!nextCursor) return;
    setCursorStack((prev) => [...prev, currentCursor]);
    setCurrentCursor(nextCursor);
  }

  function goPrev(): void {
    const prev = cursorStack[cursorStack.length - 1] ?? null;
    setCursorStack((stack) => stack.slice(0, -1));
    setCurrentCursor(prev);
  }



  const pageRows = apiRows;
  const filteredSourceSystems = sourceSystems.filter((source) => {
    const label = `${source.display_name ?? source.source_key} ${source.system_type ?? ""}`.toLowerCase();
    return label.includes(sourceSearch.toLowerCase());
  });

  const allChecked = pageRows.length > 0 && pageRows.every((p) => selected.has(p.person_id));
  const someChecked = pageRows.some((p) => selected.has(p.person_id));

  useEffect(() => {
    if (checkAllRef.current) checkAllRef.current.indeterminate = someChecked && !allChecked;
  }, [someChecked, allChecked]);

  function toggleAll(): void {
    setSelected((s) => {
      const next = new Set(s);
      if (allChecked) pageRows.forEach((p) => next.delete(p.person_id));
      else pageRows.forEach((p) => next.add(p.person_id));
      return next;
    });
  }

  function toggleOne(id: string): void {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  useEffect(() => {
    if (!relationPopover) {
      setPopoverConnections([]);
      setPopoverConnectionsTotal(null);
      return;
    }
    const controller = new AbortController();
    setPopoverConnectionsLoading(true);
    setPopoverConnections([]);
    setPopoverConnectionsTotal(null);
    void bffFetchEnvelope<PersonConnection[]>(`/bff/persons/${encodeURIComponent(relationPopover.personId)}/connections`, { signal: controller.signal })
      .then((res) => { if (!controller.signal.aborted) { setPopoverConnections(res.data); setPopoverConnectionsTotal(res.meta.total_count ?? null); } })
      .catch(() => { if (!controller.signal.aborted) { setPopoverConnections([]); } })
      .finally(() => { if (!controller.signal.aborted) { setPopoverConnectionsLoading(false); } });
    return () => controller.abort();
  }, [relationPopover?.personId]);

  useEffect(() => {
    if (!ordersPopover) {
      setPopoverOrders([]);
      setPopoverOrdersTotal(null);
      return;
    }
    const controller = new AbortController();
    setPopoverOrdersLoading(true);
    setPopoverOrders([]);
    setPopoverOrdersTotal(null);
    void bffFetchEnvelope<SalesOrder[]>(`/bff/persons/${encodeURIComponent(ordersPopover.personId)}/sales`, { signal: controller.signal })
      .then((res) => { if (!controller.signal.aborted) { setPopoverOrders(res.data); setPopoverOrdersTotal(res.meta.total_count ?? null); } })
      .catch(() => { if (!controller.signal.aborted) { setPopoverOrders([]); } })
      .finally(() => { if (!controller.signal.aborted) { setPopoverOrdersLoading(false); } });
    return () => controller.abort();
  }, [ordersPopover?.personId]);

  useEffect(() => {
    if (pageRows.length === 0) return;
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.font = `600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
    let maxNameW = 0;
    for (const p of pageRows) {
      const w = ctx.measureText(p.preferred_full_name ?? p.person_id).width;
      if (w > maxNameW) maxNameW = w;
    }
    const nameW = Math.round(Math.min(NAME_COL_MAX, Math.max(NAME_COL_MIN, maxNameW + NAME_COL_OVERHEAD)));

    ctx.font = `400 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
    let maxAddrW = 0;
    for (const p of pageRows) {
      const addr = p.preferred_address?.normalized_full ?? "";
      const w = ctx.measureText(addr).width;
      if (w > maxAddrW) maxAddrW = w;
    }
    const addrW = Math.round(Math.min(ADDR_COL_MAX, Math.max(ADDR_COL_MIN, maxAddrW + ADDR_COL_OVERHEAD)));

    setColWidths((prev) => prev.map((c, i) => {
      if (i === 1) return nameW;
      if (i === 6) return addrW;
      return c;
    }));
  }, [pageRows]);

  function closeRelationPopover(): void {
    setRelationPopover(null);
  }

  function closeOrdersPopover(): void {
    setOrdersPopover(null);
  }

  function handleRelationsClick(event: ReactMouseEvent<HTMLButtonElement>, personId: string): void {
    const anchorEl = event.currentTarget;
    const tableEl = scrollRef.current?.querySelector("table");
    const anchorRect = anchorEl.getBoundingClientRect();
    const tableRect = tableEl?.getBoundingClientRect();
    const anchorTop = tableRect ? anchorRect.bottom - tableRect.top + 8 : 0;
    const anchorRight = tableRect ? tableRect.right - anchorRect.right : 0;

    setOrdersPopover(null);
    setRelationPopover((current) => {
      if (current?.personId === personId) {
        return null;
      }
      return { personId, anchorTop, anchorRight };
    });
  }

  function handleOrdersClick(event: ReactMouseEvent<HTMLButtonElement>, personId: string): void {
    const anchorEl = event.currentTarget;
    const tableEl = scrollRef.current?.querySelector("table");
    const anchorRect = anchorEl.getBoundingClientRect();
    const tableRect = tableEl?.getBoundingClientRect();
    const anchorTop = tableRect ? anchorRect.bottom - tableRect.top + 8 : 0;
    const anchorRight = tableRect ? tableRect.right - anchorRect.right : 0;

    setRelationPopover(null);
    setOrdersPopover((current) => {
      if (current?.personId === personId) {
        return null;
      }
      return { personId, anchorTop, anchorRight };
    });
  }

  const tableWidth = colWidths.reduce((sum, width) => sum + width, 0);
  const matchFilterLabel = matchPresence === "has"
    ? "Has Recommended Match"
    : matchPresence === "none" ? "No Recommended Matches"
    : "Matches";

  return (
    <div className={styles.page}>

      {/* ── Heading ───────────────────────────────────────────── */}
      <h1 className={styles.title}>Persons</h1>

      {/* ── Stats row ─────────────────────────────────────────── */}
      <div className={styles.statsRow}>
        <button
          type="button"
          className={`${styles.statCard} ${styles.statCardClickable} ${flagFilter === "any" ? styles.statCardActive : ""}`}
          onClick={() => setFlagFilter("any")}
          title="Show all profiles"
          disabled={statsLoading}
        >
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 80, height: 10 }} /> : <div className={styles.statLabel}>Total Profiles</div>}
          {statsLoading ? (
            <span className={styles.skeletonLine} style={{ width: 64, height: 26, display: "block", marginTop: 2 }} />
          ) : (
            <div className={styles.statValue} style={{ color: "var(--good)" }}>{allProfilesCount != null ? allProfilesCount.toLocaleString() : "—"}</div>
          )}
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 70, height: 10, marginTop: 2 }} /> : <div className={styles.statSub}>active persons</div>}
        </button>

        <button
          type="button"
          className={`${styles.statCard} ${styles.statCardClickable} ${flagFilter === "high_risk" ? styles.statCardActive : ""}`}
          onClick={() => setFlagFilter((f) => f === "high_risk" ? "any" : "high_risk")}
          title="Filter by high risk"
          disabled={statsLoading}
        >
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 60, height: 10 }} /> : <div className={styles.statLabel}>High Risk</div>}
          {statsLoading ? (
            <span className={styles.skeletonLine} style={{ width: 48, height: 26, display: "block", marginTop: 2 }} />
          ) : (
            <div className={styles.statValue} style={{ color: "var(--bad)" }}>{highRiskCount != null ? highRiskCount.toLocaleString() : "—"}</div>
          )}
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 90, height: 10, marginTop: 2 }} /> : <div className={styles.statSub}>flagged as high risk</div>}
        </button>

        <button
          type="button"
          className={`${styles.statCard} ${styles.statCardClickable} ${flagFilter === "high_value" ? styles.statCardActive : ""}`}
          onClick={() => setFlagFilter((f) => f === "high_value" ? "any" : "high_value")}
          title="Filter by high value"
          disabled={statsLoading}
        >
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 68, height: 10 }} /> : <div className={styles.statLabel}>High Value</div>}
          {statsLoading ? (
            <span className={styles.skeletonLine} style={{ width: 56, height: 26, display: "block", marginTop: 2 }} />
          ) : (
            <div className={styles.statValue} style={{ color: "var(--accent)" }}>{highValueCount != null ? highValueCount.toLocaleString() : "—"}</div>
          )}
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 94, height: 10, marginTop: 2 }} /> : <div className={styles.statSub}>flagged as high value</div>}
        </button>

        <button
          type="button"
          className={`${styles.statCard} ${styles.statCardClickable} ${flagFilter === "no_contact" ? styles.statCardActive : ""}`}
          onClick={() => setFlagFilter((f) => f === "no_contact" ? "any" : "no_contact")}
          title="Filter by no contact info"
          disabled={statsLoading}
        >
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 88, height: 10 }} /> : <div className={styles.statLabel}>No contact info</div>}
          {statsLoading ? (
            <span className={styles.skeletonLine} style={{ width: 52, height: 26, display: "block", marginTop: 2 }} />
          ) : (
            <div className={styles.statValue} style={{ color: "var(--warn-text)" }}>{noContactCount != null ? noContactCount.toLocaleString() : "—"}</div>
          )}
          {statsLoading ? <span className={styles.skeletonLine} style={{ width: 76, height: 10, marginTop: 2 }} /> : <div className={styles.statSub}>can't be reached</div>}
        </button>
      </div>

      {/* ── Filter section ────────────────────────────────────────── */}
      <div className={styles.filterSection}>
        <div className={styles.searchRow}>
          <div className={styles.searchBox}>
            <svg className={styles.searchIcon} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input
              className={styles.searchInput}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name, phone, email..."
            />
          </div>
          {activeFilterCount > 0 && (
            <button className={styles.clearBtn} onClick={clearAllFilters}>Clear all</button>
          )}
        </div>

        {/* ── Filter pill bar ─────────────────────────────────────── */}
        <div className={styles.filterBar} ref={filterBarRef}>

          <FilterPill
            label="Entity"
            isActive={entityFilter.length > 0}
            activeLabel={
              entityFilter.length === 1
                ? (entities.find((e) => e.entity_key === entityFilter[0])?.display_name ?? entityFilter[0] ?? "Entity")
                : `${entityFilter.length} entities`
            }
            count={entityFilter.length > 1 ? entityFilter.length : undefined}
            onClear={() => setEntityFilter([])}
            open={openFilter === "entity"}
            onToggle={() => toggleFilter("entity")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpHeader}>
                <span className={styles.fpHeaderLabel}>Show persons from</span>
                <div className={styles.fpHeaderControls}>
                  <button
                    type="button"
                    className={`${styles.fpModeToggle} ${entityFilterMode === "and" ? styles.fpModeToggleActive : ""}`}
                    onClick={() => setEntityFilterMode((m) => (m === "and" ? "or" : "and"))}
                    disabled={entityFilter.length < 2}
                    title={entityFilter.length < 2 ? "Select 2+ entities to enable AND" : "Match all selected entities"}
                  >
                    {entityFilterMode === "and" ? "AND" : "OR"}
                  </button>
                </div>
              </div>
              {entityFilterMode === "and" && entityFilter.length < 2 && (
                <div className={styles.filterModeHint}>Select 2+ entities to use "AND"</div>
              )}
              <div className={styles.filterOptions}>
                {entityOptions.status === "loading" && <span className={styles.filterModeHint}>Loading entities...</span>}
                {entityOptions.status === "error" && (
                  <button type="button" className={styles.filterChip} onClick={entityOptions.retry}>
                    Retry entities
                  </button>
                )}
                {entityOptions.status === "loaded" && entities.length === 0 && (
                  <span className={styles.filterModeHint}>No entities available</span>
                )}
                {entities.map((entity) => (
                  <button
                    key={entity.entity_key}
                    type="button"
                    className={`${styles.filterChip} ${entityFilter.includes(entity.entity_key) ? styles.filterChipActive : ""}`}
                    onClick={() =>
                      setEntityFilter((v) =>
                        v.includes(entity.entity_key)
                          ? v.filter((k) => k !== entity.entity_key)
                          : [...v, entity.entity_key]
                      )
                    }
                  >
                    {entity.display_name ?? entity.entity_key}
                  </button>
                ))}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Source"
            isActive={sourceFilter.length > 0 || hasConversationSource}
            activeLabel={
              hasConversationSource && sourceFilter.length === 0
                ? "Chat records"
                : sourceFilter.length === 1
                  ? (sourceSystems.find((s) => s.source_key === sourceFilter[0])?.display_name ?? sourceFilter[0] ?? "Source")
                  : `${sourceFilter.length} sources`
            }
            count={sourceFilter.length > 1 ? sourceFilter.length : undefined}
            onClear={() => { setSourceFilter([]); setHasConversationSource(false); setSourceSearch(""); }}
            open={openFilter === "source"}
            onToggle={() => toggleFilter("source")}
            wide
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>Record type</div>
              <div className={styles.filterOptions}>
                <button
                  type="button"
                  className={`${styles.filterChip} ${hasConversationSource ? styles.filterChipActive : ""}`}
                  onClick={() => setHasConversationSource((current) => !current)}
                >
                  Chat / conversation
                </button>
              </div>
              <div className={styles.fpHeader}>
                <span className={styles.fpHeaderLabel}>Source system</span>
                <div className={styles.fpHeaderControls}>
                  <button
                    type="button"
                    className={`${styles.fpModeToggle} ${sourceFilterMode === "and" ? styles.fpModeToggleActive : ""}`}
                    onClick={() => setSourceFilterMode((m) => (m === "and" ? "or" : "and"))}
                    disabled={sourceFilter.length < 2}
                    title={sourceFilter.length < 2 ? "Select 2+ sources to enable AND" : "Match all selected sources"}
                  >
                    {sourceFilterMode === "and" ? "AND" : "OR"}
                  </button>
                </div>
              </div>
              {sourceFilterMode === "and" && sourceFilter.length < 2 && (
                <div className={styles.filterModeHint}>Select 2+ sources to use "AND"</div>
              )}
              <div className={styles.sourceSearchBox}>
                <svg className={styles.sourceSearchIcon} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                <input
                  className={styles.sourceSearchInput}
                  type="text"
                  value={sourceSearch}
                  onChange={(e) => setSourceSearch(e.target.value)}
                  placeholder="Search sources"
                />
              </div>
              <div className={`${styles.filterOptions} ${styles.filterOptionsScrollable} ${styles.filterOptionsStack}`}>
                {sourceOptions.status === "loading" && <span className={styles.filterModeHint}>Loading sources...</span>}
                {sourceOptions.status === "error" && (
                  <button type="button" className={styles.filterChip} onClick={sourceOptions.retry}>
                    Retry sources
                  </button>
                )}
                {sourceOptions.status === "loaded" && sourceSystems.length === 0 && (
                  <span className={styles.filterModeHint}>No source systems available</span>
                )}
                {filteredSourceSystems.map((source) => {
                  const checked = sourceFilter.includes(source.source_key);
                  return (
                    <button
                      key={source.source_key}
                      type="button"
                      title={source.display_name ?? source.source_key}
                      className={`${styles.filterChipStack} ${checked ? styles.filterChipStackActive : ""}`}
                      onClick={() =>
                        setSourceFilter((v) =>
                          checked ? v.filter((k) => k !== source.source_key) : [...v, source.source_key]
                        )
                      }
                    >
                      {source.display_name ?? source.source_key}
                    </button>
                  );
                })}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Identity"
            isActive={identityFilter.length > 0}
            activeLabel={
              identityFilter.length === 2
                ? `Email ${identityFilterMode === "or" ? "or" : "and"} Phone`
                : identityFilter.map((k) => IDENTITY_FILTERS.find((f) => f.key === k)?.label ?? k).join(", ")
            }
            onClear={() => { setIdentityFilter([]); setIdentityFilterMode("or"); }}
            open={openFilter === "identity"}
            onToggle={() => toggleFilter("identity")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpHeader}>
                <span className={styles.fpHeaderLabel}>Show persons with</span>
                <div className={styles.fpHeaderControls}>
                  <button
                    type="button"
                    className={`${styles.fpModeToggle} ${identityFilterMode === "and" ? styles.fpModeToggleActive : ""}`}
                    onClick={() => setIdentityFilterMode((m) => (m === "and" ? "or" : "and"))}
                    disabled={identityFilter.length < 2}
                    title={identityFilter.length < 2 ? "Select 2+ contact types to enable AND" : "Match all selected contact types"}
                  >
                    {identityFilterMode === "and" ? "AND" : "OR"}
                  </button>
                </div>
              </div>
              {identityFilterMode === "and" && identityFilter.length < 2 && (
                <div className={styles.filterModeHint}>Select 2+ contact types to use "AND"</div>
              )}
              <div className={styles.filterOptions}>
                {IDENTITY_FILTERS.map((filter) => {
                  const checked = identityFilter.includes(filter.key);
                  return (
                    <button
                      key={filter.key}
                      type="button"
                      className={`${styles.filterChip} ${checked ? styles.filterChipActive : ""}`}
                      onClick={() =>
                        setIdentityFilter((v) =>
                          checked ? v.filter((k) => k !== filter.key) : [...v, filter.key]
                        )
                      }
                    >
                      {filter.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Matches"
            isActive={matchPresence !== "any"}
            activeLabel={matchFilterLabel}
            onClear={() => { setMatchPresence("any"); }}
            open={openFilter === "matches"}
            onToggle={() => toggleFilter("matches")}
            wide
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>Match presence</div>
              <div className={styles.fpSegmented}>
                {(["any", "has", "none"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${matchPresence === value ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setMatchPresence(value)}
                  >
                    {value === "any" ? "Any" : value === "has" ? "Has Matches" : "No Matches"}
                  </button>
                ))}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="CRM Deals"
            isActive={crmDealRange.min !== "" || crmDealRange.max !== ""}
            activeLabel={crmDealRangeLabel(crmDealRange)}
            onClear={() => setCrmDealRange({ min: "", max: "" })}
            open={openFilter === "crmDeals"}
            onToggle={() => toggleFilter("crmDeals")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>Distinct active Bitrix CRM deals</div>
              <div className={styles.fpSegmented}>
                <button
                  type="button"
                  className={`${styles.fpSegmentedBtn} ${crmDealPreset(crmDealRange) === "any" ? styles.fpSegmentedBtnActive : ""}`}
                  onClick={() => setCrmDealRange({ min: "", max: "" })}
                >
                  Any
                </button>
                <button
                  type="button"
                  className={`${styles.fpSegmentedBtn} ${crmDealPreset(crmDealRange) === "has" ? styles.fpSegmentedBtnActive : ""}`}
                  onClick={() => setCrmDealRange({ min: "1", max: "" })}
                >
                  Has deals
                </button>
                <button
                  type="button"
                  className={`${styles.fpSegmentedBtn} ${crmDealPreset(crmDealRange) === "none" ? styles.fpSegmentedBtnActive : ""}`}
                  onClick={() => setCrmDealRange({ min: "0", max: "0" })}
                >
                  No deals
                </button>
              </div>
              <hr className={styles.fpSep} />
              <div className={styles.dateFieldsRow}>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel}>Minimum</label>
                  <div className={styles.dateInputWrap}>
                    <input
                      aria-label="Minimum CRM deals"
                      className={styles.dateInput}
                      inputMode="numeric"
                      value={crmDealRange.min}
                      onChange={(event) => setCrmDealRange((range) => ({ ...range, min: event.target.value }))}
                      placeholder="0"
                    />
                  </div>
                </div>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel}>Maximum</label>
                  <div className={styles.dateInputWrap}>
                    <input
                      aria-label="Maximum CRM deals"
                      className={styles.dateInput}
                      inputMode="numeric"
                      value={crmDealRange.max}
                      onChange={(event) => setCrmDealRange((range) => ({ ...range, max: event.target.value }))}
                      placeholder="No maximum"
                    />
                  </div>
                </div>
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Date of Birth"
            isActive={dobFilter !== "any"}
            activeLabel={dobActiveLabel()}
            onClear={clearDobFilter}
            open={openFilter === "dob"}
            onToggle={() => toggleFilter("dob")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>DOB presence</div>
              <div className={styles.fpSegmented}>
                {(["any", "has", "none"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${dobFilter === value ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setDobFilter(value)}
                  >
                    {value === "any" ? "Any" : value === "has" ? "Has DOB" : "No DOB"}
                  </button>
                ))}
              </div>
              {dobFilter === "has" && (
                <>
                  <hr className={styles.fpSep} />
                  <div className={styles.fpSectionLabel}>Match by</div>
                  <div className={styles.fpSegmented}>
                    <button
                      type="button"
                      className={`${styles.fpSegmentedBtn} ${dobMode === "single" ? styles.fpSegmentedBtnActive : ""}`}
                      onClick={() => selectDobMode("single")}
                    >
                      Pick date
                    </button>
                    <button
                      type="button"
                      className={`${styles.fpSegmentedBtn} ${dobMode === "parts" ? styles.fpSegmentedBtnActive : ""}`}
                      onClick={() => selectDobMode("parts")}
                    >
                      By parts
                    </button>
                    <button
                      type="button"
                      className={`${styles.fpSegmentedBtn} ${dobMode === "range" ? styles.fpSegmentedBtnActive : ""}`}
                      onClick={() => selectDobMode("range")}
                    >
                      Range
                    </button>
                  </div>
                  {dobMode === "single" && (
                    <div className={styles.dateInputWrap}>
                      <input
                        className={styles.dateInput}
                        type="date"
                        value={dobSingleDate}
                        onChange={(e) => setDobSingleDate(e.target.value)}
                      />
                    </div>
                  )}
                  {dobMode === "range" && (
                    <div className={styles.dateFieldsRow}>
                      <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                        <label className={styles.dateFieldLabel}>From</label>
                        <div className={styles.dateInputWrap}>
                          <input
                            aria-label="DOB start date"
                            className={styles.dateInput}
                            type="date"
                            value={dobStartDate}
                            onChange={(e) => setDobStartDate(e.target.value)}
                          />
                        </div>
                      </div>
                      <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                        <label className={styles.dateFieldLabel}>To</label>
                        <div className={styles.dateInputWrap}>
                          <input
                            aria-label="DOB end date"
                            className={styles.dateInput}
                            type="date"
                            value={dobEndDate}
                            onChange={(e) => setDobEndDate(e.target.value)}
                          />
                        </div>
                      </div>
                    </div>
                  )}
                  {dobMode === "parts" && (
                    <>
                      <div className={styles.dobPartsRow}>
                        <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                          <label className={styles.dateFieldLabel}>Year</label>
                          <div className={`${styles.dateInputWrap} ${styles.numberInputWrap}`}>
                            <input
                              aria-label="DOB year"
                              className={`${styles.dateInput} ${styles.partScheme}`}
                              type="number"
                              inputMode="numeric"
                              min={1900}
                              max={new Date().getUTCFullYear()}
                              placeholder="Any"
                              value={dobYear}
                              onChange={(e) => setDobYear(e.target.value)}
                            />
                            {dobYear && (
                              <button type="button" className={styles.fieldClear} aria-label="Clear year" onClick={() => setDobYear("")}>×</button>
                            )}
                          </div>
                        </div>
                        <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                          <label className={styles.dateFieldLabel}>Month</label>
                          <div className={styles.dateInputWrap}>
                            <select
                              aria-label="DOB month"
                              className={`${styles.dateInput} ${styles.partScheme}`}
                              value={dobMonth}
                              onChange={(e) => setDobMonth(e.target.value)}
                            >
                              <option value="">Any</option>
                              {MONTH_OPTIONS.map((m) => (
                                <option key={m.value} value={m.value}>{m.label}</option>
                              ))}
                            </select>
                            {dobMonth && (
                              <button type="button" className={styles.fieldClear} aria-label="Clear month" onClick={() => setDobMonth("")}>×</button>
                            )}
                          </div>
                        </div>
                        <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                          <label className={styles.dateFieldLabel}>Day</label>
                          <div className={styles.dateInputWrap}>
                            <select
                              aria-label="DOB day"
                              className={`${styles.dateInput} ${styles.partScheme}`}
                              value={dobDay}
                              onChange={(e) => setDobDay(e.target.value)}
                            >
                              <option value="">Any</option>
                              {DAY_OPTIONS.map((d) => (
                                <option key={d} value={d}>{Number(d)}</option>
                              ))}
                            </select>
                            {dobDay && (
                              <button type="button" className={styles.fieldClear} aria-label="Clear day" onClick={() => setDobDay("")}>×</button>
                            )}
                          </div>
                        </div>
                      </div>
                      <p className={styles.fpHint}>Combine any: e.g. Month + Day finds shared birthdays.</p>
                    </>
                  )}
                </>
              )}
            </div>
          </FilterPill>

          <FilterPill
            label="Address"
            isActive={addressFilter !== "any" || addrCity !== "" || addrPostal !== "" || addrCountry !== ""}
            activeLabel={
              addrCity || addrPostal || addrCountry
                ? [addrCity, addrPostal, addrCountry].filter(Boolean).join(", ")
                : addressFilter === "none" ? "No address" : "Has address"
            }
            onClear={() => { setAddressFilter("any"); setAddrCity(""); setAddrPostal(""); setAddrCountry(""); }}
            open={openFilter === "address"}
            onToggle={() => toggleFilter("address")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>Has address</div>
              <div className={styles.fpSegmented}>
                {(["any", "has", "none"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${addressFilter === value ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setAddressFilter(value)}
                  >
                    {value === "any" ? "Any" : value === "has" ? "Has" : "None"}
                  </button>
                ))}
              </div>
              {addressFilter === "has" && (
              <>
              <hr className={styles.fpSep} />
              <div className={styles.fpSectionLabel}>Location details</div>
              <div className={styles.dateFieldGroup}>
                <label className={styles.dateFieldLabel}>City</label>
                <div className={styles.dateInputWrap}>
                  <input
                    className={styles.dateInput}
                    type="text"
                    value={addrCity}
                    onChange={(e) => setAddrCity(e.target.value)}
                    placeholder="e.g. Singapore"
                  />
                </div>
              </div>
              <div className={styles.dateFieldsRow}>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel}>Postal</label>
                  <div className={styles.dateInputWrap}>
                    <input
                      className={styles.dateInput}
                      type="text"
                      value={addrPostal}
                      onChange={(e) => setAddrPostal(e.target.value)}
                      placeholder="e.g. 018982"
                    />
                  </div>
                </div>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel}>Country</label>
                  <div className={styles.dateInputWrap}>
                    <input
                      className={styles.dateInput}
                      type="text"
                      value={addrCountry}
                      onChange={(e) => setAddrCountry(e.target.value)}
                      placeholder="e.g. SG"
                    />
                  </div>
                </div>
              </div>
              </>
              )}
            </div>
          </FilterPill>

          <FilterPill
            label="Flags"
            isActive={flagFilter !== "any"}
            activeLabel={flagFilter === "high_value" ? "High Value" : flagFilter === "no_contact" ? "No Contact Info" : "High Risk"}
            onClear={() => setFlagFilter("any")}
            open={openFilter === "flags"}
            onToggle={() => toggleFilter("flags")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>Person flag</div>
              <div className={styles.fpSegmented}>
                {(["any", "high_value", "high_risk"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${flagFilter === value ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setFlagFilter(value)}
                  >
                    {value === "any" ? "Any" : value === "high_value" ? "High Value" : "High Risk"}
                  </button>
                ))}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Bankruptcy"
            isActive={hasBankruptcy !== null}
            activeLabel="Has case"
            onClear={() => setHasBankruptcy(null)}
            open={openFilter === "bankruptcy"}
            onToggle={() => toggleFilter("bankruptcy")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSectionLabel}>Bankruptcy case</div>
              <div className={styles.fpSegmented}>
                {([null, true] as const).map((value) => (
                  <button
                    key={String(value)}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${hasBankruptcy === value ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setHasBankruptcy(value)}
                  >
                    {value === null ? "Any" : "Has case"}
                  </button>
                ))}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Updated"
            isActive={updatedAfter !== "" || updatedBefore !== ""}
            activeLabel={
              updatedAfter && updatedBefore ? `${updatedAfter} → ${updatedBefore}`
                : updatedAfter ? `After ${updatedAfter}`
                : `Before ${updatedBefore}`
            }
            onClear={() => { setUpdatedAfter(""); setUpdatedBefore(""); }}
            open={openFilter === "updated"}
            onToggle={() => toggleFilter("updated")}
          >
            <div className={styles.fpInner}>
              <div className={styles.dateFieldsRow}>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel}>From</label>
                  <div className={styles.dateInputWrap}>
                    <input
                      aria-label="Updated after"
                      className={styles.dateInput}
                      type="date"
                      value={updatedAfter}
                      onChange={(e) => setUpdatedAfter(e.target.value)}
                    />
                  </div>
                </div>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel}>To</label>
                  <div className={styles.dateInputWrap}>
                    <input
                      aria-label="Updated before"
                      className={styles.dateInput}
                      type="date"
                      value={updatedBefore}
                      onChange={(e) => setUpdatedBefore(e.target.value)}
                    />
                  </div>
                </div>
              </div>
            </div>
          </FilterPill>


        </div>
        {/* ── Count bar + Table ─────────────────────────────────── */}
        <div className={styles.tableSection}>
          <PaginationBar
            showing={paginationShowing}
            hasPrev={cursorStack.length > 0} hasNext={nextCursor !== null}
            pageSize={pageSize} selectedCount={selected.size}
            onPrev={goPrev} onNext={goNext}
            onPageSize={(n) => { setPageSize(n); setCurrentCursor(null); setCursorStack([]); }}
          />
          <div className={`${styles.tableScrollWrap} ${canScrollRight ? styles.canScrollRight : ""} ${canScrollLeft ? styles.canScrollLeft : ""}`}>
            <div className={styles.scrollHint}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="9 18 15 12 9 6"/>
              </svg>
              Scroll to see more
            </div>
          <div className={styles.tableScroll} ref={scrollRef}>
          <div className={styles.tableContent} style={{ minWidth: tableWidth }}>
          <table
            className={styles.table}
            style={{ minWidth: tableWidth }}
            onMouseDown={handleTableMouseDown}
            onMouseMove={handleTableMouseMove}
            onMouseLeave={(e) => { e.currentTarget.style.cursor = ""; }}
          >
            <colgroup>
              {colWidths.map((w, i) => (
                <col key={i} ref={(el) => { colEls.current[i] = el; }} style={{ width: w }} />
              ))}
            </colgroup>
            <thead>
              <tr>
                <th className={`${styles.th} ${styles.thCheck}`}>
                  <input type="checkbox" ref={checkAllRef} checked={allChecked} onChange={toggleAll} className={styles.checkbox} />
                </th>
                {(["Name", "NRIC", "Phone", "Date of Birth", "Email", "Address", "Entity", "Relations", "Orders", "CRM Deals", "Matches", "Decision History"] as const).map((h, i) => {
                  const sk: SortKey | null = h === "Name" ? "name" : h === "Date of Birth" ? "dob" : h === "Entity" ? "entity" : h === "Relations" ? "relations" : h === "Matches" ? "matches" : h === "Decision History" ? "decisionHistory" : h === "Orders" ? "orders" : h === "CRM Deals" ? "deals" : null;
                  return (
                    <th key={h} className={styles.th}>
                      {sk ? (
                        <button className={styles.sortBtn} onClick={() => toggleSort(sk)}>
                          {h}
                          <SortIcon active={sortKey === sk} dir={sortDir} />
                        </button>
                      ) : h}
                      <div className={styles.resizeHandle} onMouseDown={(e) => startColResize(i + 1, e)} />
                    </th>
                  );
                })}
                <th className={`${styles.th} ${styles.th}`}>
                  <button className={styles.sortBtn} onClick={() => toggleSort("quality")}>
                    Completeness
                    <SortIcon active={sortKey === "quality"} dir={sortDir} />
                  </button>
                  <div className={styles.resizeHandle} onMouseDown={(e) => startColResize(13, e)} />
                </th>
                <th className={`${styles.th} ${styles.thSticky} ${styles.stickyGraph}`}>Graph</th>
              </tr>
            </thead>
            <tbody>
              {fetchLoading ? (
                Array.from({ length: pageSize > 25 ? 12 : 8 }, (_, i) => (
                  <SkeletonRow key={i} index={i} colWidths={colWidths} />
                ))
              ) : fetchError ? (
                <tr><td colSpan={PERSON_LIST_TABLE_COLUMN_COUNT} className={styles.empty} style={{ color: "var(--bad)" }}>{fetchError}</td></tr>
              ) : pageRows.length === 0 ? (
                <tr>
                  <td colSpan={PERSON_LIST_TABLE_COLUMN_COUNT} className={styles.empty}>No persons match your filters.</td>
                </tr>
              ) : (
                pageRows.map((p) => (
                  <PersonRow
                    key={p.person_id}
                    p={p}
                    checked={selected.has(p.person_id)}
                    onToggle={() => toggleOne(p.person_id)}
                    onEntityClick={(key) =>
                      setEntityFilter((value) => (value.includes(key) ? value : [...value, key]))
                    }
                    onRelationsClick={handleRelationsClick}
                    onOrdersClick={handleOrdersClick}
                    onGraphClick={(personId, name) => setGraphDialog({ open: true, personId, title: name })}
                    isResizing={() => suppressRowClickRef.current}
                  />
                ))
              )}
            </tbody>
          </table>
          {relationPopover && (popoverConnectionsLoading || popoverConnections.length > 0) && (
            <RelationPopover
              personId={relationPopover.personId}
              anchorTop={relationPopover.anchorTop}
              anchorRight={relationPopover.anchorRight}
              connections={popoverConnections}
              loading={popoverConnectionsLoading}
              total={popoverConnectionsTotal}
              onClose={closeRelationPopover}
            />
          )}
          {ordersPopover && (popoverOrdersLoading || popoverOrders.length > 0) && (
            <OrdersPopover
              personId={ordersPopover.personId}
              anchorTop={ordersPopover.anchorTop}
              anchorRight={ordersPopover.anchorRight}
              orders={popoverOrders}
              loading={popoverOrdersLoading}
              total={popoverOrdersTotal}
              onClose={closeOrdersPopover}
            />
          )}
          </div>{/* tableContent */}
          </div>{/* tableScroll */}
          </div>{/* tableScrollWrap */}

          {/* ── Mobile card list — hidden on desktop ─────────── */}
          <div className={styles.mobileList}>
            {fetchLoading ? (
              Array.from({ length: 8 }, (_, i) => (
                <div key={i} className={styles.mobileSkeletonCard}>
                  <span className={styles.skeletonAvatar} />
                  <div className={styles.mobileSkeletonBody}>
                    <span className={styles.skeletonLine} style={{ width: "55%" }} />
                    <span className={styles.skeletonLine} style={{ width: "35%", height: 10 }} />
                  </div>
                  <div className={styles.mobileSkeletonRight}>
                    <span className={styles.skeletonLine} style={{ width: 50, height: 10 }} />
                    <span className={styles.skeletonLine} style={{ width: 40, height: 10 }} />
                  </div>
                </div>
              ))
            ) : fetchError ? (
              <p className={styles.mobileEmpty} style={{ color: "var(--bad)" }}>{fetchError}</p>
            ) : pageRows.length === 0 ? (
              <p className={styles.mobileEmpty}>No persons match your filters.</p>
            ) : (
              pageRows.map((p) => (
                <PersonCardMobile
                  key={p.person_id}
                  p={p}
                  onGraphClick={(personId, name) => setGraphDialog({ open: true, personId, title: name })}
                />
              ))
            )}
          </div>

          <PaginationBar
            showing={paginationShowing}
            hasPrev={cursorStack.length > 0} hasNext={nextCursor !== null}
            pageSize={pageSize} selectedCount={selected.size}
            onPrev={goPrev} onNext={goNext}
            onPageSize={(n) => { setPageSize(n); setCurrentCursor(null); setCursorStack([]); }}
            bottom
          />
        </div>
      </div>{/* end main card */}
      <PersonGraphDialog
        open={graphDialog.open}
        personId={graphDialog.personId}
        title={graphDialog.title}
        onClose={() => setGraphDialog((g) => ({ ...g, open: false }))}
      />
    </div>
  );
}

export default function PersonsPage(): ReactElement {
  return (
    <Suspense fallback={null}>
      <PersonsInner />
    </Suspense>
  );
}
// hot reload test marker 1782110348
