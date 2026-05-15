"use client";

import { useState, useEffect, useRef, useCallback, useMemo, type MouseEvent as ReactMouseEvent, type ReactElement, type ReactNode } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { MOCK_ENTITIES, MOCK_PERSON_CONNECTIONS_BY_PERSON_ID, MOCK_SALES, MOCK_SOURCE_SYSTEMS } from "@/lib/mock-data";
import type { ListedPerson, PersonConnection, SalesOrder } from "@/lib/api-types";
import { bffFetchEnvelope, BffError } from "@/lib/api-client";
import styles from "./persons.module.css";


interface ColDef { key: string; minWidth: number; resizable: boolean }
const COLS: ColDef[] = [
  { key: "check",   minWidth: 36,  resizable: false },
  { key: "name",    minWidth: 120, resizable: true  },
  { key: "nric",    minWidth: 80,  resizable: true  },
  { key: "phone",   minWidth: 80,  resizable: true  },
  { key: "dob",     minWidth: 90,  resizable: true  },
  { key: "email",   minWidth: 100, resizable: true  },
  { key: "address", minWidth: 120, resizable: true  },
  { key: "entity",  minWidth: 80,  resizable: true  },
  { key: "relations", minWidth: 96, resizable: true },
  { key: "orders", minWidth: 84, resizable: true },
  { key: "quality", minWidth: 108, resizable: true  },
  { key: "graph",   minWidth: 48,  resizable: false },
];
const DEFAULT_WIDTHS = [36, 180, 110, 110, 110, 160, 200, 120, 112, 84, 124, 54];

const AVATAR_COLORS = ["#4361ee", "#7c3aed", "#0891b2", "#059669", "#d97706", "#dc2626"];

function avatarColor(name: string): string {
  return AVATAR_COLORS[name.charCodeAt(0) % AVATAR_COLORS.length] ?? "#4361ee";
}

function qualityColor(s: number): string {
  if (s >= 0.8) return "#22c55e";
  if (s >= 0.5) return "#f59e0b";
  return "#ef4444";
}

function parseDob(dob: string | null): { display: string; invalid: boolean } {
  if (!dob) return { display: "—", invalid: false };
  const year = parseInt(dob.slice(0, 4), 10);
  if (year < 1920 || year > 2026) return { display: dob, invalid: true };
  const d = new Date(dob + "T00:00:00");
  return {
    display: d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }),
    invalid: false,
  };
}

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

type RelationTypeOption = {
  key: string;
  label: string;
};

function formatRelationCategory(category: string): string {
  return category
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function relationTypeKey(kind: "category" | "label", value: string): string {
  return `${kind}:${value}`;
}

function collectConnectionRelationTypes(connection: PersonConnection): RelationTypeOption[] {
  return connection.knows_relationships.flatMap((relationship) => {
    const options: RelationTypeOption[] = [
      {
        key: relationTypeKey("category", relationship.relationship_category),
        label: formatRelationCategory(relationship.relationship_category),
      },
    ];

    if (relationship.relationship_label) {
      options.push({
        key: relationTypeKey("label", relationship.relationship_label),
        label: relationship.relationship_label,
      });
    }

    return options;
  });
}

function uniqueRelationTypeOptions(options: RelationTypeOption[]): RelationTypeOption[] {
  const seen = new Set<string>();
  return options.filter((option) => {
    if (seen.has(option.key)) return false;
    seen.add(option.key);
    return true;
  });
}

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

const FALLBACK_RELATION_LABELS = [
  "Emergency contact",
  "Family contact",
  "Referral contact",
  "Co-borrower",
  "Shared household",
  "Business contact",
] as const;

const FALLBACK_RELATION_STATUSES = ["active", "active", "active", "merged", "suppressed"] as const;

function buildFallbackConnections(person: ListedPerson): PersonConnection[] {
  const baseName = person.preferred_full_name?.split(" ").slice(0, 2).join(" ") ?? "Related profile";

  return Array.from({ length: person.connection_count }, (_, index) => ({
    person_id: `rel-${person.person_id}-${String(index + 1).padStart(2, "0")}`,
    status: FALLBACK_RELATION_STATUSES[index % FALLBACK_RELATION_STATUSES.length] ?? "active",
    preferred_full_name: `${baseName} ${String.fromCharCode(66 + (index % 8))}`,
    hops: index % 4 === 3 ? 2 : 1,
    shared_identifiers: index % 3 === 0 && person.preferred_phone
      ? [{ identifier_type: "phone", normalized_value: person.preferred_phone }]
      : [],
    shared_addresses: index % 3 === 1 && person.preferred_address
      ? [{ address_id: person.preferred_address.address_id, normalized_full: person.preferred_address.normalized_full }]
      : [],
    knows_relationships: [{
      relationship_label: FALLBACK_RELATION_LABELS[index % FALLBACK_RELATION_LABELS.length] ?? "Related contact",
      relationship_category: index % 3 === 0 ? "family" : index % 3 === 1 ? "household" : "business",
    }],
  }));
}

function getPersonConnections(person: ListedPerson): PersonConnection[] {
  const explicitConnections = MOCK_PERSON_CONNECTIONS_BY_PERSON_ID[person.person_id];
  if (explicitConnections && explicitConnections.length > 0) {
    return explicitConnections;
  }
  if (person.connection_count <= 0) {
    return [];
  }
  return buildFallbackConnections(person);
}

function getRelationTypeOptions(persons: ListedPerson[]): RelationTypeOption[] {
  return uniqueRelationTypeOptions(
    persons.flatMap((person) => getPersonConnections(person).flatMap(collectConnectionRelationTypes)),
  ).sort((left, right) => left.label.localeCompare(right.label));
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
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(`${date}T00:00:00`));
}

function buildFallbackOrders(person: ListedPerson): SalesOrder[] {
  return Array.from({ length: person.order_count }, (_, index) => ({
    order_no: `ORD-${person.person_id.toUpperCase()}-${String(index + 1).padStart(3, "0")}`,
    source_order_id: `SRC-${String(index + 1).padStart(4, "0")}`,
    order_date: `2026-${String((index % 9) + 1).padStart(2, "0")}-${String(((index * 3) % 27) + 1).padStart(2, "0")}`,
    release_date: null,
    total_amount: 49 + index * 38.5,
    currency: "SGD",
    source_system: index % 2 === 0 ? "speedzone-phppos" : "fundbox-bitrix",
    entity_name: person.entities[0]?.display_name ?? "SpeedZone Retail",
    line_items: [{
      line_no: 1,
      quantity: (index % 3) + 1,
      unit_price: 49 + index * 38.5,
      subtotal: 49 + index * 38.5,
      product: {
        display_name: index % 2 === 0 ? "Running Shoes" : "Membership Package",
        sku: index % 2 === 0 ? `SKU-RUN-${index + 1}` : `SKU-MEM-${index + 1}`,
        category: index % 2 === 0 ? "Footwear" : "Services",
      },
    }],
  }));
}

function getPersonOrders(person: ListedPerson): SalesOrder[] {
  if (person.person_id === "p-001") {
    return MOCK_SALES;
  }
  if (person.order_count <= 0) {
    return [];
  }
  return buildFallbackOrders(person);
}

function OrdersPopover({
  personId,
  anchorTop,
  anchorRight,
  orders,
  onClose,
}: {
  personId: string;
  anchorTop: number;
  anchorRight: number;
  orders: SalesOrder[];
  onClose: () => void;
}): ReactElement {
  const popoverRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent): void {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (popoverRef.current?.contains(target)) return;
      onClose();
    }

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

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
            {orders.length} order{orders.length === 1 ? "" : "s"}
          </div>
        </div>
        <button type="button" className={styles.ordersPopoverClose} onClick={onClose} aria-label="Close orders popover">×</button>
      </div>
      <div className={styles.ordersList}>
        {orders.map((order, index) => {
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
    </div>
  );
}

function RelationPopover({
  personId,
  anchorTop,
  anchorRight,
  connections,
  onClose,
}: {
  personId: string;
  anchorTop: number;
  anchorRight: number;
  connections: PersonConnection[];
  onClose: () => void;
}): ReactElement {
  const popoverRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent): void {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (popoverRef.current?.contains(target)) return;
      onClose();
    }

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

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
            {connections.length} linked profile{connections.length === 1 ? "" : "s"}
          </div>
        </div>
        <button type="button" className={styles.relationsPopoverClose} onClick={onClose} aria-label="Close relations popover">
          ×
        </button>
      </div>
      <div className={styles.relationsList}>
        {connections.map((connection) => (
          <div key={connection.person_id} className={styles.relationItem}>
            <div className={styles.relationItemTop}>
              <Link href={`/persons/${connection.person_id}`} className={styles.relationLink} onClick={onClose}>
                {connection.preferred_full_name ?? connection.person_id}
              </Link>
            </div>
            <div className={styles.relationMetaRow}>
              <span className={styles.relationMetaId} title={connection.person_id}>{connection.person_id}</span>
            </div>
            <div className={styles.relationSummary}>{formatRelationType(connection)}</div>
          </div>
        ))}
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
  const ringColor = qualityColor(score);
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
}: {
  p: ListedPerson;
  checked: boolean;
  onToggle: () => void;
  onEntityClick: (key: string) => void;
  onRelationsClick: (event: ReactMouseEvent<HTMLButtonElement>, personId: string) => void;
  onOrdersClick: (event: ReactMouseEvent<HTMLButtonElement>, personId: string) => void;
}): ReactElement {
  const initials = (p.preferred_full_name ?? "?")
    .split(" ")
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
  const pct = Math.round(p.profile_completeness_score * 100);
  const dob = parseDob(p.preferred_dob);
  const labels = getLabels(p);

  return (
    <tr className={styles.tr}>
      <td className={styles.tdCheck} onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" checked={checked} onChange={onToggle} className={styles.checkbox} />
      </td>
      <td className={`${styles.td} ${styles.tdName}`}>
        <Link href={`/persons/${p.person_id}`} className={styles.personLink}>
          <div className={styles.personCell}>
            <AvatarRing initials={initials} color={avatarColor(p.preferred_full_name ?? "?")} score={p.profile_completeness_score} />
            <div className={styles.personInfo}>
              <div className={styles.personName} title={p.preferred_full_name ?? p.person_id}>{p.preferred_full_name ?? p.person_id}</div>
              <div className={styles.personMeta}>
                <span className={styles.personId} title={p.person_id}>
                  {p.person_id.length > 10 ? `${p.person_id.slice(0, 8)}…` : p.person_id}
                </span>
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
          <span className={dob.invalid ? styles.dobBad : styles.dobText}>{dob.display}</span>
          {dob.invalid && (
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
      <td className={`${styles.td} ${styles.tdSticky} ${styles.stickyQuality}`}>
        <div className={styles.qualityWrap}>
          <div className={styles.qualityBar}>
            <div
              className={styles.qualityFill}
              style={{ width: `${pct}%`, background: qualityColor(p.profile_completeness_score) }}
            />
          </div>
          <span className={styles.qualityText}>{pct}%</span>
        </div>
      </td>
      <td className={`${styles.tdGraph} ${styles.tdSticky} ${styles.stickyGraph}`}>
        <Link href={`/relationships?person=${p.person_id}`} className={styles.graphLink} title="View in graph" onClick={(e) => e.stopPropagation()}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/>
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>
          </svg>
        </Link>
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

type FilterKey = "status" | "entity" | "source" | "identity" | "dob" | "address" | "relations" | "quality" | "sales";

function FilterPill({
  label,
  isActive,
  activeLabel,
  count,
  onClear,
  open,
  onToggle,
  alignRight,
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
        <div className={`${styles.filterPopover} ${alignRight ? styles.filterPopoverRight : ""}`}>
          {children}
        </div>
      )}
    </div>
  );
}

type StatusFilter = "active" | "merged" | "suppressed";
type PresenceFilter = "any" | "has" | "none";
type DobMode = "single" | "range";
type SortKey = "name" | "dob" | "orders" | "quality";
type SortDir = "asc" | "desc";

const STATUS_FILTERS: StatusFilter[] = ["active", "merged", "suppressed"];

const IDENTITY_FILTERS = [
  { key: "email", label: "Email" },
  { key: "phone", label: "Phone" },
  { key: "nric", label: "NRIC" },
] as const;


function PersonsInner(): ReactElement {
  const searchParams = useSearchParams();
  const [search, setSearch] = useState(searchParams.get("q") ?? "");

  const [statusFilter, setStatusFilter] = useState<StatusFilter | null>(null);
  const [entityFilter, setEntityFilter] = useState<string[]>([]);
  const [sourceFilter, setSourceFilter] = useState<string[]>([]);
  const [sourceSearch, setSourceSearch] = useState("");
  const [identityFilter, setIdentityFilter] = useState<string[]>([]);
  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const [addressFilter, setAddressFilter] = useState<PresenceFilter>("any");
  const [relationFilter, setRelationFilter] = useState<PresenceFilter>("any");
  const [relationTypeFilter, setRelationTypeFilter] = useState<string[]>([]);
  const [dobFilter, setDobFilter] = useState<PresenceFilter>("any");
  const [dobMode, setDobMode] = useState<DobMode>("single");
  const [dobSingleDate, setDobSingleDate] = useState("");
  const [dobStartDate, setDobStartDate] = useState("");
  const [dobEndDate, setDobEndDate] = useState("");
  const [qualityMin, setQualityMin] = useState(0);
  const [qualityMax, setQualityMax] = useState(100);
  const [hasOrdersOnly, setHasOrdersOnly] = useState(false);
  const [threePlusOrdersOnly, setThreePlusOrdersOnly] = useState(false);
  const [orderTotalMin, setOrderTotalMin] = useState("");
  const [orderTotalMax, setOrderTotalMax] = useState("");
  const [openFilter, setOpenFilter] = useState<FilterKey | null>(null);
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [apiRows, setApiRows] = useState<ListedPerson[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [fetchLoading, setFetchLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([]);
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);


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
  const [pageSize, setPageSize] = useState(100);
  const [canScrollRight, setCanScrollRight] = useState(false);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [colWidths, setColWidths] = useState<number[]>(DEFAULT_WIDTHS);
  const [relationPopover, setRelationPopover] = useState<RelationPopoverState | null>(null);
  const [ordersPopover, setOrdersPopover] = useState<OrdersPopoverState | null>(null);
  const checkAllRef = useRef<HTMLInputElement | null>(null);
  const filterBarRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const colEls = useRef<(HTMLTableColElement | null)[]>([]);
  const dragRef = useRef<{ idx: number; startX: number; startW: number } | null>(null);

  function startColResize(idx: number, e: React.MouseEvent): void {
    e.preventDefault();
    const startW = colWidths[idx] ?? DEFAULT_WIDTHS[idx] ?? 100;
    dragRef.current = { idx, startX: e.clientX, startW };

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
    setOpenFilter((prev) => (prev === key ? null : key));
  }

  const activeFilterCount = [
    statusFilter,
    verifiedOnly,
    addressFilter !== "any",
    relationFilter !== "any",
    relationTypeFilter.length > 0,
    dobFilter !== "any",
    dobFilter === "has" && dobMode === "single" && dobSingleDate !== "",
    dobFilter === "has" && dobMode === "range" && (dobStartDate !== "" || dobEndDate !== ""),
    qualityMin > 0 || qualityMax < 100,
    hasOrdersOnly,
    threePlusOrdersOnly,
    orderTotalMin !== "",
    orderTotalMax !== "",
  ].filter(Boolean).length + entityFilter.length + sourceFilter.length + identityFilter.length;

  function clearAllFilters(): void {
    setStatusFilter(null);
    setEntityFilter([]);
    setSourceFilter([]);
    setSourceSearch("");
    setIdentityFilter([]);
    setVerifiedOnly(false);
    setAddressFilter("any");
    setRelationFilter("any");
    setRelationTypeFilter([]);
    setDobFilter("any");
    setDobMode("single");
    setDobSingleDate("");
    setDobStartDate("");
    setDobEndDate("");
    setQualityMin(0);
    setQualityMax(100);
    setHasOrdersOnly(false);
    setThreePlusOrdersOnly(false);
    setOrderTotalMin("");
    setOrderTotalMax("");
  }

  const apiQuery = useMemo(() => {
    const params = new URLSearchParams();
    if (search.trim().length >= 3) params.set("q", search.trim());
    entityFilter.forEach((key) => params.append("entity_key", key));
    sourceFilter.forEach((key) => params.append("source_key", key));
    if (identityFilter.includes("email")) params.set("has_email", "true");
    if (identityFilter.includes("phone")) params.set("has_phone", "true");
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
      }
    } else if (dobFilter === "none") {
      params.set("has_dob", "false");
    }
    const sortByMap: Record<SortKey, string> = {
      name: "preferred_full_name",
      dob: "preferred_dob",
      orders: "order_count",
      quality: "profile_completeness_score",
    };
    if (sortKey) {
      params.set("sort_by", sortByMap[sortKey]);
      params.set("sort_order", sortDir);
    }
    params.set("limit", String(pageSize));
    return params.toString();
  }, [search, entityFilter, sourceFilter, identityFilter, addressFilter, dobFilter, dobMode, dobSingleDate, dobStartDate, dobEndDate, sortKey, sortDir, pageSize]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCurrentCursor(null);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCursorStack([]);
  }, [apiQuery]);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFetchLoading(true);
    setFetchError(null);
    const params = new URLSearchParams(apiQuery);
    if (currentCursor) params.set("cursor", currentCursor);
    const run = async (): Promise<void> => {
      try {
        const envelope = await bffFetchEnvelope<ListedPerson[]>(`/bff/persons?${params.toString()}`);
        if (!cancelled) {
          setApiRows(envelope.data ?? []);
          setTotal(envelope.meta.total_count ?? null);
          setNextCursor(envelope.meta.next_cursor ?? null);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setFetchError(err instanceof BffError ? err.message : "Failed to load data.");
          setApiRows([]);
        }
      } finally {
        if (!cancelled) setFetchLoading(false);
      }
    };
    void run();
    return () => { cancelled = true; };
  }, [apiQuery, currentCursor]);

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


  const relationTypeOptions = getRelationTypeOptions(apiRows);

  const pageRows = apiRows;
  const filteredSourceSystems = MOCK_SOURCE_SYSTEMS.filter((source) => {
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
  const activePopoverPerson = relationPopover ? apiRows.find((person) => person.person_id === relationPopover.personId) ?? null : null;
  const activeConnections = activePopoverPerson ? getPersonConnections(activePopoverPerson) : [];
  const activeOrdersPerson = ordersPopover ? apiRows.find((person) => person.person_id === ordersPopover.personId) ?? null : null;
  const activeOrders = activeOrdersPerson ? getPersonOrders(activeOrdersPerson) : [];

  return (
    <div className={styles.page}>

      {/* ── Heading ───────────────────────────────────────────── */}
      <h1 className={styles.title}>Persons</h1>

      {/* ── Stats row ─────────────────────────────────────────── */}
      <div className={styles.statsRow}>
        <div className={styles.statCard}>
          <div className={styles.statLabel}>Total Profiles</div>
          <div className={styles.statValue} style={{ color: "var(--good)" }}>{total != null ? total.toLocaleString() : "—"}</div>
          <div className={styles.statSub}>+128 this week</div>
        </div>

        <div className={styles.statCard}>
          <div className={styles.statLabel}>Completeness</div>
          <div className={styles.statValue} style={{ color: "var(--warn-text)" }}>78%</div>
          <div className={styles.statSub}>203 records flagged</div>
        </div>

        <div className={styles.statCard}>
          <div className={styles.statLabel}>Birthdays this month</div>
          <div className={styles.statValue} style={{ color: "var(--accent)" }}>47</div>
          <div className={styles.statSub}>in {new Date().toLocaleString("en", { month: "long" })}</div>
        </div>

        <div className={styles.statCard}>
          <div className={styles.statLabel}>No contact info</div>
          <div className={styles.statValue} style={{ color: "var(--bad)" }}>341</div>
          <div className={styles.statSub}>Can&apos;t be reached — fix needed</div>
        </div>
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
            label="Status"
            isActive={statusFilter !== null}
            activeLabel={statusFilter ? (statusFilter.charAt(0).toUpperCase() + statusFilter.slice(1)) : ""}
            onClear={() => setStatusFilter(null)}
            open={openFilter === "status"}
            onToggle={() => toggleFilter("status")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSegmented}>
                {STATUS_FILTERS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${statusFilter === s ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setStatusFilter((v) => (v === s ? null : s))}
                  >
                    {s.charAt(0).toUpperCase() + s.slice(1)}
                  </button>
                ))}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Entity"
            isActive={entityFilter.length > 0}
            activeLabel={
              entityFilter.length === 1
                ? (MOCK_ENTITIES.find((e) => e.entity_key === entityFilter[0])?.display_name ?? "Entity")
                : `${entityFilter.length} entities`
            }
            count={entityFilter.length > 1 ? entityFilter.length : undefined}
            onClear={() => setEntityFilter([])}
            open={openFilter === "entity"}
            onToggle={() => toggleFilter("entity")}
          >
            <div className={styles.fpInner}>
              <div className={styles.filterOptions}>
                {MOCK_ENTITIES.map((entity) => (
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
            isActive={sourceFilter.length > 0}
            activeLabel={
              sourceFilter.length === 1
                ? (MOCK_SOURCE_SYSTEMS.find((s) => s.source_key === sourceFilter[0])?.display_name ?? "Source")
                : `${sourceFilter.length} sources`
            }
            count={sourceFilter.length > 1 ? sourceFilter.length : undefined}
            onClear={() => { setSourceFilter([]); setSourceSearch(""); }}
            open={openFilter === "source"}
            onToggle={() => toggleFilter("source")}
          >
            <div className={styles.fpInner}>
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
              <div className={`${styles.filterOptions} ${styles.filterOptionsScrollable}`}>
                {filteredSourceSystems.map((source) => {
                  const checked = sourceFilter.includes(source.source_key);
                  return (
                    <button
                      key={source.source_key}
                      type="button"
                      className={`${styles.filterChip} ${checked ? styles.filterChipActive : ""}`}
                      onClick={() =>
                        setSourceFilter((v) =>
                          checked ? v.filter((k) => k !== source.source_key) : [...v, source.source_key]
                        )
                      }
                    >
                      {source.display_name}
                    </button>
                  );
                })}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Identity"
            isActive={identityFilter.length > 0 || verifiedOnly}
            activeLabel={
              identityFilter.length > 0
                ? identityFilter.map((k) => IDENTITY_FILTERS.find((f) => f.key === k)?.label ?? k).join(", ")
                : "Verified only"
            }
            onClear={() => { setIdentityFilter([]); setVerifiedOnly(false); }}
            open={openFilter === "identity"}
            onToggle={() => toggleFilter("identity")}
          >
            <div className={styles.fpInner}>
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
              <label className={styles.switch}>
                <input
                  type="checkbox"
                  className={styles.switchInput}
                  checked={verifiedOnly}
                  onChange={(e) => setVerifiedOnly(e.target.checked)}
                />
                <span className={styles.switchTrack} aria-hidden="true">
                  <span className={styles.switchThumb} />
                </span>
                <span className={styles.switchLabel}>Verified only</span>
              </label>
            </div>
          </FilterPill>

          <FilterPill
            label="Date of Birth"
            isActive={dobFilter !== "any"}
            activeLabel={
              dobFilter === "none"
                ? "No DOB"
                : dobMode === "single" && dobSingleDate
                  ? `DOB: ${dobSingleDate}`
                  : dobMode === "range" && (dobStartDate || dobEndDate)
                    ? `DOB: ${dobStartDate || "…"} → ${dobEndDate || "…"}`
                    : "Has DOB"
            }
            onClear={() => { setDobFilter("any"); setDobMode("single"); setDobSingleDate(""); setDobStartDate(""); setDobEndDate(""); }}
            open={openFilter === "dob"}
            onToggle={() => toggleFilter("dob")}
          >
            <div className={styles.fpInner}>
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
                  <div className={styles.fpSegmented}>
                    <button
                      type="button"
                      className={`${styles.fpSegmentedBtn} ${dobMode === "single" ? styles.fpSegmentedBtnActive : ""}`}
                      onClick={() => { setDobMode("single"); setDobStartDate(""); setDobEndDate(""); }}
                    >
                      Pick date
                    </button>
                    <button
                      type="button"
                      className={`${styles.fpSegmentedBtn} ${dobMode === "range" ? styles.fpSegmentedBtnActive : ""}`}
                      onClick={() => { setDobMode("range"); setDobSingleDate(""); }}
                    >
                      Range
                    </button>
                  </div>
                  {dobMode === "single" ? (
                    <div className={styles.dateInputWrap}>
                      <input
                        className={styles.dateInput}
                        type="date"
                        value={dobSingleDate}
                        onChange={(e) => setDobSingleDate(e.target.value)}
                      />
                    </div>
                  ) : (
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
                </>
              )}
            </div>
          </FilterPill>

          <FilterPill
            label="Address"
            isActive={addressFilter !== "any"}
            activeLabel={addressFilter === "none" ? "No address" : "Has address"}
            onClear={() => setAddressFilter("any")}
            open={openFilter === "address"}
            onToggle={() => toggleFilter("address")}
          >
            <div className={styles.fpInner}>
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
            </div>
          </FilterPill>

          <FilterPill
            label="Relations"
            isActive={relationFilter !== "any" || relationTypeFilter.length > 0}
            activeLabel={
              relationFilter === "none"
                ? "No relations"
                : relationTypeFilter.length > 0
                  ? `Has: ${relationTypeFilter.length} type${relationTypeFilter.length === 1 ? "" : "s"}`
                  : "Has relations"
            }
            onClear={() => { setRelationFilter("any"); setRelationTypeFilter([]); }}
            open={openFilter === "relations"}
            onToggle={() => toggleFilter("relations")}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpSegmented}>
                {(["any", "has", "none"] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={`${styles.fpSegmentedBtn} ${relationFilter === value ? styles.fpSegmentedBtnActive : ""}`}
                    onClick={() => setRelationFilter(value)}
                  >
                    {value === "any" ? "Any" : value === "has" ? "Has" : "None"}
                  </button>
                ))}
              </div>
              <hr className={styles.fpSep} />
              <span className={styles.fpSectionLabel}>Types</span>
              <div className={`${styles.filterOptions} ${styles.filterOptionsScrollable}`}>
                {relationTypeOptions.map((option) => {
                  const checked = relationTypeFilter.includes(option.key);
                  return (
                    <button
                      key={option.key}
                      type="button"
                      className={`${styles.filterChip} ${checked ? styles.filterChipActive : ""}`}
                      onClick={() =>
                        setRelationTypeFilter((v) =>
                          checked ? v.filter((k) => k !== option.key) : [...v, option.key]
                        )
                      }
                    >
                      {option.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Quality"
            isActive={qualityMin > 0 || qualityMax < 100}
            activeLabel={`${qualityMin}–${qualityMax}%`}
            onClear={() => { setQualityMin(0); setQualityMax(100); }}
            open={openFilter === "quality"}
            onToggle={() => toggleFilter("quality")}
            alignRight
          >
            <div className={styles.fpInner}>
              <div className={styles.qualityRangeHeader}>
                <span className={styles.qualityRangeValue}>{qualityMin}%</span>
                <span className={styles.qualityRangeDash}>–</span>
                <span className={styles.qualityRangeValue}>{qualityMax}%</span>
              </div>
              <div className={styles.qualitySliderStack}>
                <div className={styles.qualitySliderRail} aria-hidden="true" />
                <div
                  className={styles.qualitySliderRange}
                  aria-hidden="true"
                  style={{ left: `${qualityMin}%`, width: `${Math.max(qualityMax - qualityMin, 0)}%` }}
                />
                <input
                  className={`${styles.rangeInput} ${styles.rangeInputMin}`}
                  type="range" min="0" max="100" value={qualityMin}
                  onChange={(e) => setQualityMin(Math.min(Number(e.target.value), qualityMax))}
                />
                <input
                  className={`${styles.rangeInput} ${styles.rangeInputMax}`}
                  type="range" min="0" max="100" value={qualityMax}
                  onChange={(e) => setQualityMax(Math.max(Number(e.target.value), qualityMin))}
                />
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Sales"
            isActive={hasOrdersOnly || threePlusOrdersOnly || orderTotalMin !== "" || orderTotalMax !== ""}
            activeLabel={
              hasOrdersOnly ? "Has orders" : threePlusOrdersOnly ? "≥3 orders" : "Order range"
            }
            onClear={() => { setHasOrdersOnly(false); setThreePlusOrdersOnly(false); setOrderTotalMin(""); setOrderTotalMax(""); }}
            open={openFilter === "sales"}
            onToggle={() => toggleFilter("sales")}
            alignRight
          >
            <div className={styles.fpInner}>
              <div className={styles.filterOptions}>
                <button
                  type="button"
                  className={`${styles.filterChip} ${hasOrdersOnly ? styles.filterChipActive : ""}`}
                  onClick={() => setHasOrdersOnly((v) => !v)}
                >
                  Has orders
                </button>
                <button
                  type="button"
                  className={`${styles.filterChip} ${threePlusOrdersOnly ? styles.filterChipActive : ""}`}
                  onClick={() => setThreePlusOrdersOnly((v) => !v)}
                >
                  ≥3 orders
                </button>
              </div>
              <div className={styles.dateFieldsRow}>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel} htmlFor="fp-order-min">Min total</label>
                  <div className={`${styles.dateInputWrap} ${styles.numberInputWrap}`}>
                    <input
                      id="fp-order-min"
                      aria-label="Minimum order total"
                      className={styles.dateInput}
                      min="0" type="number"
                      value={orderTotalMin}
                      onChange={(e) => setOrderTotalMin(e.target.value)}
                    />
                  </div>
                </div>
                <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                  <label className={styles.dateFieldLabel} htmlFor="fp-order-max">Max total</label>
                  <div className={`${styles.dateInputWrap} ${styles.numberInputWrap}`}>
                    <input
                      id="fp-order-max"
                      aria-label="Maximum order total"
                      className={styles.dateInput}
                      min="0" type="number"
                      value={orderTotalMax}
                      onChange={(e) => setOrderTotalMax(e.target.value)}
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
            showing={`Showing ${apiRows.length === 0 ? 0 : cursorStack.length * pageSize + 1}–${cursorStack.length * pageSize + apiRows.length} of ${total != null ? total.toLocaleString() : "…"} profiles`}
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
                {(["Name", "NRIC", "Phone", "Date of Birth", "Email", "Address", "Entity", "Relations", "Orders"] as const).map((h, i) => {
                  const sk: SortKey | null = h === "Name" ? "name" : h === "Date of Birth" ? "dob" : h === "Orders" ? "orders" : null;
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
                <th className={`${styles.th} ${styles.thSticky} ${styles.stickyQuality}`}>
                  <button className={styles.sortBtn} onClick={() => toggleSort("quality")}>
                    Completeness
                    <SortIcon active={sortKey === "quality"} dir={sortDir} />
                  </button>
                  <div className={styles.resizeHandle} onMouseDown={(e) => startColResize(10, e)} />
                </th>
                <th className={`${styles.th} ${styles.thSticky} ${styles.stickyGraph}`}>Graph</th>
              </tr>
            </thead>
            <tbody>
              {fetchLoading ? (
                <tr><td colSpan={12} className={styles.empty}>Loading…</td></tr>
              ) : fetchError ? (
                <tr><td colSpan={12} className={styles.empty} style={{ color: "var(--bad)" }}>{fetchError}</td></tr>
              ) : pageRows.length === 0 ? (
                <tr>
                  <td colSpan={12} className={styles.empty}>No persons match your filters.</td>
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
                  />
                ))
              )}
            </tbody>
          </table>
          {relationPopover && activeConnections.length > 0 && (
            <RelationPopover
              personId={relationPopover.personId}
              anchorTop={relationPopover.anchorTop}
              anchorRight={relationPopover.anchorRight}
              connections={activeConnections}
              onClose={closeRelationPopover}
            />
          )}
          {ordersPopover && activeOrders.length > 0 && (
            <OrdersPopover
              personId={ordersPopover.personId}
              anchorTop={ordersPopover.anchorTop}
              anchorRight={ordersPopover.anchorRight}
              orders={activeOrders}
              onClose={closeOrdersPopover}
            />
          )}
          </div>{/* tableContent */}
          </div>{/* tableScroll */}
          </div>{/* tableScrollWrap */}

          <PaginationBar
            showing={`Showing ${apiRows.length === 0 ? 0 : cursorStack.length * pageSize + 1}–${cursorStack.length * pageSize + apiRows.length} of ${total != null ? total.toLocaleString() : "…"} profiles`}
            hasPrev={cursorStack.length > 0} hasNext={nextCursor !== null}
            pageSize={pageSize} selectedCount={selected.size}
            onPrev={goPrev} onNext={goNext}
            onPageSize={(n) => { setPageSize(n); setCurrentCursor(null); setCursorStack([]); }}
            bottom
          />
        </div>
      </div>{/* end main card */}
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
