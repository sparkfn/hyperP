"use client";

import { useState, useEffect, useRef, useCallback, type MouseEvent as ReactMouseEvent, type ReactElement } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { MOCK_PERSONS, MOCK_ENTITIES, MOCK_PERSON_CONNECTIONS_BY_PERSON_ID, MOCK_SALES, MOCK_SOURCE_SYSTEMS } from "@/lib/mock-data";
import type { ListedPerson, PersonConnection, PersonEntitySummary, SalesOrder } from "@/lib/api-types";
import styles from "./persons.module.css";

const TOTAL = 6039;

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

// Generated mock persons — chaotic realistic data for edge-case testing
const NAMES = [
  // Very long Malay names
  "Muhammad Nur Hakim Bin Abdul Rahman Al-Rashid",
  "Siti Noor Hafizah Binte Mohd Zulkifli",
  "Mohamad Faris Irfan Bin Haji Mohd Suffian",
  "Nur Aisyah Binte Muhammad Hafidz",
  "Muhammad Asyraaf Bin Mohd Norzaini",
  // Long Indian names
  "Balasubramaniam s/o Krishnaswamy Naicker",
  "Thavaseelan s/o Rajasekaran",
  "Letchumy d/o Govindasamy Pillai",
  "Subramaniam Venkatesan s/o Muthuswamy",
  "Vijayalakshmi d/o Ramasamy",
  // Long Chinese names
  "Chua Ah Kow @ Jeffrey Chua Beng Huat",
  "Tan Wei Ming Brandon",
  "Lim Siew Kuan Jasmine",
  "Wong Boon Huat Michael",
  "Ng Swee Leng Doris",
  // Short / weird names (bad data)
  "Li",
  "Unknown Unknown",
  "Test User",
  "N/A",
  "CUSTOMER 00291",
  // Normal SG names
  "Ahmad Razif Bin Osman","Chua Beng Teck","Lim Mei Ling","Tan Ah Kow","Wong Siew Fong",
  "Ng Boon Huat","Lee Shu Fen","Goh Chee Keong","Yeo Pei Shan","Ong Kah Wee",
  "Fatimah Binte Yusof","Zainuddin Bin Mohd","Ho Wai Kien","Chong Kok Leong",
  "Teo Beng Seng","Lau Ah Lian","Koh Swee Leng","Sim Hui Ying","Ang Bee Lian",
  "Fong Weng Fatt","Chin Siew Lan","Leong Wai Mun","Quek Ah Huat","Heng Soo Khim",
  "Yap Kim Huat","Seow Boon Lay","Mohamad Hafiz Bin Ali","Noor Hidayah Binte Rahman",
  "Deepa d/o Rajan","Selvam s/o Krishnan","Kavitha Devi","Murugesan s/o Perumal",
  "Chan Wai Leng","Tay Boon Pin","Wee Siew Khim","Ibrahim Bin Kassim",
  "Nurhayati Binte Salleh","Hairul Anuar Bin Taib","Vasu s/o Pillai","Saroja d/o Ramu",
  "Bernard Tan","Vincent Lim","Raymond Ng","Patrick Lee","Anthony Goh",
  "Michelle Wong","Stephanie Chua","Jennifer Teo","Samantha Koh","Rebecca Yeo",
  "Darren Ang","Justin Fong","Marcus Chin","Wayne Leong","Ethan Quek",
];

const LONG_ADDRESSES = [
  "Block 123 Jurong West Street 41 #05-12, Singapore 640123",
  "1 Marina Bay Financial Centre Tower 3 #42-01, Singapore 018982",
  "25 Serangoon North Avenue 5 #08-33 Riverfront Residences, Singapore 554025",
  "Blk 456A Tampines Street 44 #12-345, Singapore 522456",
  "88 Pasir Ris Drive 10 #03-02, Singapore 519078",
  "321 Bukit Timah Road #07-11 Heng Leong Building, Singapore 227820",
  null, null, null,
];

const LONG_EMAILS = [
  null, null,
  "muhammad.nur.hakim@verylongcompanynamecorporation.com.sg",
  "balasubramaniam.krishnaswamy@enterprise-solutions-group.sg",
  "user@example.sg",
  "test.customer.profile.001@gmail.com",
  "no-reply-bounced-address@invalid",
];

const ENTITY_LIST: PersonEntitySummary[][] = [
  [{ entity_key: "speedzone", display_name: "SpeedZone Retail", entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 1 }],
  [{ entity_key: "fundbox",   display_name: "Fundbox Consumer",  entity_type: "finance",   country_code: "SG", is_active: true, source_record_count: 1 }],
  [{ entity_key: "eko",       display_name: "Eko Commerce",      entity_type: "ecommerce", country_code: "SG", is_active: true, source_record_count: 1 }],
  [
    { entity_key: "speedzone", display_name: "SpeedZone Retail", entity_type: "retail",  country_code: "SG", is_active: true, source_record_count: 3 },
    { entity_key: "fundbox",   display_name: "Fundbox Consumer", entity_type: "finance", country_code: "SG", is_active: true, source_record_count: 2 },
    { entity_key: "eko",       display_name: "Eko Commerce",     entity_type: "ecommerce", country_code: "SG", is_active: true, source_record_count: 1 },
  ],
  [],
  [{ entity_key: "bitrix-sg", display_name: "Bitrix SG CRM", entity_type: "crm", country_code: "SG", is_active: false, source_record_count: 1 }],
];

const BAD_DOBS = ["1900-01-01", "2099-12-31", "1111-11-11", "0001-01-01"];
const STATUSES: ListedPerson["status"][] = ["active","active","active","active","active","merged","merged","suppressed"];

function makePerson(i: number): ListedPerson {
  const name = NAMES[i % NAMES.length] ?? `Person ${i}`;
  const yr = 1948 + (i * 11 % 60);
  const mo = String((i % 12) + 1).padStart(2, "0");
  const dy = String((i % 28) + 1).padStart(2, "0");
  // Varied completeness: some very low, some high
  const scoreRaw = [0.08, 0.15, 0.22, 0.31, 0.44, 0.55, 0.62, 0.71, 0.80, 0.88, 0.93, 0.97];
  const score = scoreRaw[i % scoreRaw.length] ?? 0.5;
  const nricPrefix = yr < 2000 ? "S" : "T";
  const entities = ENTITY_LIST[i % ENTITY_LIST.length] ?? [];
  // Various messy phone formats
  const phones = [null, null, `+65 ${8000 + (i % 999)} ${String(i * 37 % 9999).padStart(4,"0")}`,
    `65${9000 + (i % 999)}${i % 9999}`, `+60 12-${i % 9999} ${i % 9999}`, `(+65) 9${i%9}${i%9}${i%9}-${i%9}${i%9}${i%9}${i%9}`];
  const phone = phones[i % phones.length] ?? null;
  const email = LONG_EMAILS[i % LONG_EMAILS.length] ?? null;
  const addr = LONG_ADDRESSES[i % LONG_ADDRESSES.length];
  const badDob = BAD_DOBS[Math.floor(i / 12) % BAD_DOBS.length] ?? null;
  const dob = i % 12 === 0 ? badDob : (i % 8 === 0 ? null : `${yr}-${mo}-${dy}`);

  const hex = (n: number, len: number) => n.toString(16).padStart(len, "0");
  const uuid = `${hex(i * 0xdeadbeef & 0xffffffff, 8)}-${hex(i * 0x1234 & 0xffff, 4)}-4${hex(i * 0xabcd & 0xfff, 3)}-${hex((i * 0x5678 & 0x3fff) | 0x8000, 4)}-${hex(i * 0xfeedface & 0xffffffffffff, 12)}`;
  return {
    person_id: uuid,
    status: STATUSES[i % STATUSES.length] ?? "active",
    is_high_value: i % 7 === 0,
    is_high_risk:  i % 13 === 0,
    preferred_full_name: i % 20 === 0 ? null : name,
    preferred_phone: phone,
    preferred_email: email,
    preferred_dob: dob,
    preferred_nric: i % 5 !== 0
      ? `${nricPrefix}${String(yr % 100).padStart(2,"0")}${String((i * 1234) % 99999).padStart(5,"0")}${String.fromCharCode(65 + (i % 26))}`
      : null,
    preferred_address: addr
      ? { address_id: `ag-${i}`, unit_number: null, street_number: null, street_name: null, city: "Singapore", postal_code: null, country_code: "SG", normalized_full: addr }
      : null,
    profile_completeness_score: score,
    golden_profile_computed_at: score > 0.5 ? "2026-04-01T00:00:00Z" : null,
    golden_profile_version: score > 0.5 ? "1" : null,
    source_record_count: 1 + (i % 8),
    connection_count: i % 10,
    created_at: `202${4 + (i % 3)}-${String((i % 12) + 1).padStart(2,"0")}-01T00:00:00Z`,
    updated_at: "2026-04-01T00:00:00Z",
    phone_confidence: phone ? 0.4 + (i % 6) * 0.1 : null,
    entities,
    entity_count: entities.length,
    identifier_count: 1 + (i % 6),
    order_count: [0,0,1,2,3,5,8,13,21,34][i % 10] ?? 0,
  };
}

const GENERATED = Array.from({ length: 120 }, (_, i) => makePerson(i + 11));

const ALL_PERSONS = [...MOCK_PERSONS, ...GENERATED];

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

function getPersonRelationTypeKeys(person: ListedPerson): string[] {
  return getPersonConnections(person).flatMap((connection) => collectConnectionRelationTypes(connection).map((option) => option.key));
}

function getRelationTypeOptions(persons: ListedPerson[]): RelationTypeOption[] {
  return uniqueRelationTypeOptions(
    persons.flatMap((person) => getPersonConnections(person).flatMap(collectConnectionRelationTypes)),
  ).sort((left, right) => left.label.localeCompare(right.label));
}

function getRelationTypeLabel(options: RelationTypeOption[], key: string): string {
  return options.find((option) => option.key === key)?.label ?? key;
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

function getPersonOrderTotal(person: ListedPerson): number {
  return getPersonOrders(person).reduce((total, order) => total + (order.total_amount ?? 0), 0);
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
  showing, page, totalPages, pageSize, selectedCount, onPrev, onNext, onPageSize, bottom,
}: {
  showing: string; page: number; totalPages: number; pageSize: number; selectedCount: number;
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
        {totalPages > 1 && (
          <>
            <button className={styles.pageBtn} disabled={page === 0} onClick={onPrev}>← Prev</button>
            <span className={styles.pageInfo}>Page {page + 1} of {totalPages}</span>
            <button className={styles.pageBtn} disabled={page >= totalPages - 1} onClick={onNext}>Next →</button>
          </>
        )}
      </div>
    </div>
  );
}

type StatusFilter = "active" | "merged" | "suppressed";
type PresenceFilter = "any" | "has" | "none";
type DobMode = "single" | "range";

const STATUS_FILTERS: StatusFilter[] = ["active", "merged", "suppressed"];

const IDENTITY_FILTERS = [
  { key: "email", label: "Email" },
  { key: "phone", label: "Phone" },
  { key: "nric", label: "NRIC" },
] as const;

const ENTITY_SOURCE_KEYS: Record<string, string[]> = {
  speedzone: ["speedzone-phppos"],
  fundbox: ["fundbox-bitrix"],
  eko: ["eko-phppos"],
  "bitrix-sg": ["bitrix-sg"],
};

function getPersonSourceKeys(person: ListedPerson): string[] {
  const keys = new Set<string>();

  person.entities.forEach((entity) => {
    ENTITY_SOURCE_KEYS[entity.entity_key]?.forEach((key) => keys.add(key));
  });

  if (person.preferred_phone || person.identifier_count >= 3) {
    keys.add("whatsapp-main");
  }

  if (keys.size === 0) {
    keys.add("whatsapp-main");
  }

  return Array.from(keys);
}

function getPersonIdentityTypes(person: ListedPerson): string[] {
  const types = new Set<string>();

  if (person.preferred_email) types.add("email");
  if (person.preferred_phone) types.add("phone");
  if (person.preferred_nric) types.add("nric");

  return Array.from(types);
}

function hasVerifiedIdentity(person: ListedPerson, type: string): boolean {
  switch (type) {
    case "email":
      return Boolean(person.preferred_email);
    case "phone":
      return Boolean(person.preferred_phone);
    case "nric":
      return Boolean(person.preferred_nric);
    default:
      return false;
  }
}

function dobMatches(personDob: string | null, mode: DobMode, singleDate: string, startDate: string, endDate: string): boolean {
  if (!personDob) return false;

  if (mode === "single") {
    return singleDate ? personDob === singleDate : true;
  }

  if (startDate && personDob < startDate) return false;
  if (endDate && personDob > endDate) return false;
  return startDate !== "" || endDate !== "";
}

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
  const [showFilters, setShowFilters] = useState(false);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(100);
  const [canScrollRight, setCanScrollRight] = useState(false);
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [colWidths, setColWidths] = useState<number[]>(DEFAULT_WIDTHS);
  const [relationPopover, setRelationPopover] = useState<RelationPopoverState | null>(null);
  const [ordersPopover, setOrdersPopover] = useState<OrdersPopoverState | null>(null);
  const checkAllRef = useRef<HTMLInputElement | null>(null);
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

  // Reset to page 0 whenever filters change
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPage(0);
  }, [search, statusFilter, entityFilter, sourceFilter, identityFilter, verifiedOnly, addressFilter, relationFilter, relationTypeFilter, dobFilter, dobMode, dobSingleDate, dobStartDate, dobEndDate, qualityMin, qualityMax, hasOrdersOnly, threePlusOrdersOnly, orderTotalMin, orderTotalMax]);

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

  const relationTypeOptions = getRelationTypeOptions(ALL_PERSONS);

  const filtered = ALL_PERSONS.filter((p) => {
    if (search) {
      const q = search.toLowerCase();
      if (
        !p.preferred_full_name?.toLowerCase().includes(q) &&
        !p.preferred_email?.toLowerCase().includes(q) &&
        !p.preferred_phone?.includes(q)
      )
        return false;
    }
    if (statusFilter && p.status !== statusFilter) return false;
    if (entityFilter.length > 0 && !p.entities.some((e) => entityFilter.includes(e.entity_key))) return false;
    if (sourceFilter.length > 0 && !getPersonSourceKeys(p).some((key) => sourceFilter.includes(key))) return false;
    if (identityFilter.length > 0) {
      const personIdentityTypes = getPersonIdentityTypes(p);
      if (!identityFilter.some((type) => personIdentityTypes.includes(type))) return false;
      if (verifiedOnly && !identityFilter.some((type) => hasVerifiedIdentity(p, type))) return false;
    } else if (verifiedOnly) {
      if (!["email", "phone", "nric"].some((type) => hasVerifiedIdentity(p, type))) return false;
    }
    if (addressFilter === "has" && !p.preferred_address) return false;
    if (addressFilter === "none" && p.preferred_address) return false;
    if (relationFilter === "has" && p.connection_count <= 0) return false;
    if (relationFilter === "none" && p.connection_count > 0) return false;
    if (relationTypeFilter.length > 0) {
      const relationTypeKeys = getPersonRelationTypeKeys(p);
      if (!relationTypeFilter.some((key) => relationTypeKeys.includes(key))) return false;
    }
    if (hasOrdersOnly && p.order_count <= 0) return false;
    if (threePlusOrdersOnly && p.order_count < 3) return false;
    const minOrderTotal = Number(orderTotalMin);
    const maxOrderTotal = Number(orderTotalMax);
    if ((Number.isFinite(minOrderTotal) && orderTotalMin !== "") || (Number.isFinite(maxOrderTotal) && orderTotalMax !== "")) {
      const orderTotal = getPersonOrderTotal(p);
      if (Number.isFinite(minOrderTotal) && orderTotalMin !== "" && orderTotal < minOrderTotal) return false;
      if (Number.isFinite(maxOrderTotal) && orderTotalMax !== "" && orderTotal > maxOrderTotal) return false;
    }
    if (dobFilter === "has") {
      if (!p.preferred_dob) return false;
      if ((dobMode === "single" && dobSingleDate !== "") || (dobMode === "range" && (dobStartDate !== "" || dobEndDate !== ""))) {
        if (!dobMatches(p.preferred_dob, dobMode, dobSingleDate, dobStartDate, dobEndDate)) return false;
      }
    }
    if (dobFilter === "none" && p.preferred_dob) return false;
    const qualityScore = Math.round(p.profile_completeness_score * 100);
    if (qualityScore < qualityMin || qualityScore > qualityMax) return false;
    return true;
  });

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, totalPages - 1);
  const pageStart = safePage * pageSize;
  const pageEnd = Math.min(pageStart + pageSize, filtered.length);
  const pageRows = filtered.slice(pageStart, pageEnd);
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
  const activePopoverPerson = relationPopover ? ALL_PERSONS.find((person) => person.person_id === relationPopover.personId) ?? null : null;
  const activeConnections = activePopoverPerson ? getPersonConnections(activePopoverPerson) : [];
  const activeOrdersPerson = ordersPopover ? ALL_PERSONS.find((person) => person.person_id === ordersPopover.personId) ?? null : null;
  const activeOrders = activeOrdersPerson ? getPersonOrders(activeOrdersPerson) : [];

  return (
    <div className={styles.page}>

      {/* ── Heading ───────────────────────────────────────────── */}
      <h1 className={styles.title}>Persons</h1>

      {/* ── Stats row ─────────────────────────────────────────── */}
      <div className={styles.statsRow}>
        <div className={styles.statCard}>
          <svg className={styles.statBg} viewBox="0 0 24 24" fill="currentColor">
            <path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/>
          </svg>
          <div className={styles.statLabel}>Total Profiles</div>
          <div className={styles.statValue} style={{ color: "#16a34a" }}>{TOTAL.toLocaleString()}</div>
          <div className={styles.statSub}>+128 this week</div>
        </div>

        <div className={styles.statCard}>
          <svg className={styles.statBg} viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 22C6.477 22 2 17.523 2 12S6.477 2 12 2s10 4.477 10 10-4.477 10-10 10zm-1.177-7.86l-2.765-2.767L7 12.431l3.823 3.845L18 9.124l-1.06-1.06-6.117 6.077z"/>
          </svg>
          <div className={styles.statLabel}>Completeness</div>
          <div className={styles.statValue} style={{ color: "#d97706" }}>78%</div>
          <div className={styles.statSub}>203 records flagged</div>
        </div>

        <div className={styles.statCard}>
          <svg className={styles.statBg} viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 6c1.11 0 2-.89 2-2 0-.74-.4-1.39-1-1.73V1h-2v1.27C10.4 2.61 10 3.26 10 4c0 1.11.89 2 2 2zm4.6 9.99l-1.07-1.07-1.08 1.07c-1.3 1.3-3.58 1.31-4.89 0l-1.07-1.07-1.09 1.07C6.75 16.64 5.88 17 4.96 17c-.73 0-1.4-.23-1.96-.61V21c0 .55.45 1 1 1h16c.55 0 1-.45 1-1v-4.61c-.56.38-1.23.61-1.96.61-.92 0-1.79-.36-2.44-1.01zM18 9H6c-1.66 0-3 1.34-3 3v1.54c0 1.08.88 1.96 1.96 1.96.52 0 1.02-.2 1.38-.57l2.14-2.13 2.13 2.13c.74.74 2.03.74 2.77 0l2.14-2.13 2.13 2.13c.37.37.86.57 1.38.57 1.08 0 1.96-.88 1.96-1.96V12C21 10.34 19.66 9 18 9z"/>
          </svg>
          <div className={styles.statLabel}>Birthdays this month</div>
          <div className={styles.statValue} style={{ color: "#1d4ed8" }}>47</div>
          <div className={styles.statSub}>in {new Date().toLocaleString("en", { month: "long" })}</div>
        </div>

        <div className={styles.statCard}>
          <svg className={styles.statBg} viewBox="0 0 24 24" fill="currentColor">
            <path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1-9.4 0-17-7.6-17-17 0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1L6.6 10.8z"/>
          </svg>
          <div className={styles.statLabel}>No contact info</div>
          <div className={styles.statValue} style={{ color: "#dc2626" }}>341</div>
          <div className={styles.statSub}>Can&apos;t be reached – FIX needed</div>
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
          <button
            className={`${styles.filterToggleBtn} ${showFilters ? styles.filterToggleBtnActive : ""}`}
            type="button"
            onClick={() => setShowFilters((value) => !value)}
            aria-expanded={showFilters}
            aria-controls="persons-filter-groups"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>
            </svg>
            Filter
            {activeFilterCount > 0 && <span className={styles.filterToggleCount}>{activeFilterCount}</span>}
          </button>
          {activeFilterCount > 0 && (
            <button className={styles.clearBtn} onClick={clearAllFilters}>Clear filters</button>
          )}
        </div>

        <div className={styles.activeBadges}>
          {statusFilter && (
            <span className={styles.activeBadge}>
              {statusFilter}
              <button className={styles.badgeX} onClick={() => setStatusFilter(null)}>×</button>
            </span>
          )}
          {entityFilter.map((entityKey) => (
            <span key={entityKey} className={styles.activeBadge}>
              {MOCK_ENTITIES.find((e) => e.entity_key === entityKey)?.display_name ?? entityKey}
              <button
                className={styles.badgeX}
                onClick={() => setEntityFilter((value) => value.filter((key) => key !== entityKey))}
              >
                ×
              </button>
            </span>
          ))}
          {identityFilter.map((identityKey) => (
            <span key={identityKey} className={styles.activeBadge}>
              {IDENTITY_FILTERS.find((filter) => filter.key === identityKey)?.label ?? identityKey}
              <button
                className={styles.badgeX}
                onClick={() => setIdentityFilter((value) => value.filter((key) => key !== identityKey))}
              >
                ×
              </button>
            </span>
          ))}
          {verifiedOnly && (
            <span className={styles.activeBadge}>
              Verified only
              <button className={styles.badgeX} onClick={() => setVerifiedOnly(false)}>×</button>
            </span>
          )}
          {sourceFilter.map((sourceKey) => (
            <span key={sourceKey} className={styles.activeBadge}>
              {MOCK_SOURCE_SYSTEMS.find((source) => source.source_key === sourceKey)?.display_name ?? sourceKey}
              <button
                className={styles.badgeX}
                onClick={() => setSourceFilter((value) => value.filter((key) => key !== sourceKey))}
              >
                ×
              </button>
            </span>
          ))}
          {addressFilter !== "any" && (
            <span className={styles.activeBadge}>
              Address: {addressFilter === "has" ? "Has" : "None"}
              <button className={styles.badgeX} onClick={() => setAddressFilter("any")}>×</button>
            </span>
          )}
          {relationFilter !== "any" && (
            <span className={styles.activeBadge}>
              Relations: {relationFilter === "has" ? "Has" : "None"}
              <button className={styles.badgeX} onClick={() => setRelationFilter("any")}>×</button>
            </span>
          )}
          {relationTypeFilter.length > 0 && (
            <span className={styles.activeBadge}>
              {relationTypeFilter.length === 1
                ? `Relation: ${getRelationTypeLabel(relationTypeOptions, relationTypeFilter[0] ?? "")}`
                : `Relations: ${relationTypeFilter.length} types`}
              <button className={styles.badgeX} onClick={() => setRelationTypeFilter([])}>×</button>
            </span>
          )}
          {hasOrdersOnly && <span className={styles.activeBadge}>Has orders<button className={styles.badgeX} onClick={() => setHasOrdersOnly(false)}>×</button></span>}
          {threePlusOrdersOnly && <span className={styles.activeBadge}>≥3 orders<button className={styles.badgeX} onClick={() => setThreePlusOrdersOnly(false)}>×</button></span>}
          {orderTotalMin !== "" && <span className={styles.activeBadge}>Min total: {orderTotalMin}<button className={styles.badgeX} onClick={() => setOrderTotalMin("")}>×</button></span>}
          {orderTotalMax !== "" && <span className={styles.activeBadge}>Max total: {orderTotalMax}<button className={styles.badgeX} onClick={() => setOrderTotalMax("")}>×</button></span>}
          {dobFilter !== "any" && (
            <span className={styles.activeBadge}>
              {dobFilter === "none"
                ? "DOB: None"
                : dobMode === "single" && dobSingleDate
                  ? `DOB: ${dobSingleDate}`
                  : dobMode === "range" && (dobStartDate || dobEndDate)
                    ? `DOB: ${dobStartDate || "…"} → ${dobEndDate || "…"}`
                    : "DOB: Has"}
              <button
                className={styles.badgeX}
                onClick={() => {
                  setDobFilter("any");
                  setDobMode("single");
                  setDobSingleDate("");
                  setDobStartDate("");
                  setDobEndDate("");
                }}
              >
                ×
              </button>
            </span>
          )}
          {(qualityMin > 0 || qualityMax < 100) && (
            <span className={styles.activeBadge}>
              Quality: {qualityMin}–{qualityMax}
              <button
                className={styles.badgeX}
                onClick={() => {
                  setQualityMin(0);
                  setQualityMax(100);
                }}
              >
                ×
              </button>
            </span>
          )}
        </div>

        {showFilters && (
          <div className={styles.filterGroups} id="persons-filter-groups">
            <div className={styles.filterRowStack}>
              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Status</span>
                </div>
                <div className={styles.filterOptions}>
                  {STATUS_FILTERS.map((status) => (
                    <button
                      key={status}
                      className={`${styles.filterChip} ${statusFilter === status ? styles.filterChipActive : ""}`}
                      onClick={() => setStatusFilter((value) => value === status ? null : status)}
                    >
                      {status.charAt(0).toUpperCase() + status.slice(1)}
                    </button>
                  ))}
                </div>
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>DOB</span>
                </div>
                <div className={styles.filterGroupBody}>
                  <div className={styles.filterOptions}>
                    {(["any", "has", "none"] as const).map((value) => (
                      <button
                        key={value}
                        type="button"
                        className={`${styles.filterChip} ${dobFilter === value ? styles.filterChipActive : ""}`}
                        onClick={() => setDobFilter(value)}
                      >
                        {value === "any" ? "Any" : value === "has" ? "Has" : "None"}
                      </button>
                    ))}
                  </div>
                  {dobFilter === "has" && (
                    <>
                      <div className={styles.filterOptions}>
                        <button
                          type="button"
                          className={`${styles.filterChip} ${dobMode === "single" ? styles.filterChipActive : ""}`}
                          onClick={() => {
                            setDobMode("single");
                            setDobStartDate("");
                            setDobEndDate("");
                          }}
                        >
                          Pick date
                        </button>
                        <button
                          type="button"
                          className={`${styles.filterChip} ${dobMode === "range" ? styles.filterChipActive : ""}`}
                          onClick={() => {
                            setDobMode("range");
                            setDobSingleDate("");
                          }}
                        >
                          Range
                        </button>
                      </div>
                      {dobMode === "single" ? (
                        <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                          <label className={styles.dateFieldLabel} htmlFor="dob-single-date">Pick date</label>
                          <div className={styles.dateInputWrap}>
                            <input
                              id="dob-single-date"
                              className={styles.dateInput}
                              type="date"
                              value={dobSingleDate}
                              onChange={(e) => setDobSingleDate(e.target.value)}
                            />
                          </div>
                        </div>
                      ) : (
                        <div className={styles.dateFieldsRow}>
                          <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                            <label className={styles.dateFieldLabel} htmlFor="dob-start-date">From</label>
                            <div className={styles.dateInputWrap}>
                              <input
                                id="dob-start-date"
                                aria-label="DOB start date"
                                className={styles.dateInput}
                                type="date"
                                value={dobStartDate}
                                onChange={(e) => setDobStartDate(e.target.value)}
                              />
                            </div>
                          </div>
                          <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                            <label className={styles.dateFieldLabel} htmlFor="dob-end-date">To</label>
                            <div className={styles.dateInputWrap}>
                              <input
                                id="dob-end-date"
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
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Address</span>
                </div>
                <div className={styles.filterOptions}>
                  {(["any", "has", "none"] as const).map((value) => (
                    <button
                      key={value}
                      type="button"
                      className={`${styles.filterChip} ${addressFilter === value ? styles.filterChipActive : ""}`}
                      onClick={() => setAddressFilter(value)}
                    >
                      {value === "any" ? "Any" : value === "has" ? "Has" : "None"}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className={styles.filterRowStack}>
              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Identity</span>
                </div>
                <div className={styles.filterGroupBody}>
                  <div className={styles.filterOptions}>
                    {IDENTITY_FILTERS.map((filter) => {
                      const checked = identityFilter.includes(filter.key);
                      return (
                        <button
                          key={filter.key}
                          type="button"
                          className={`${styles.filterChip} ${checked ? styles.filterChipActive : ""}`}
                          onClick={() =>
                            setIdentityFilter(
                              checked
                                ? identityFilter.filter((key) => key !== filter.key)
                                : [...identityFilter, filter.key]
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
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Sales</span>
                </div>
                <div className={styles.filterGroupBody}>
                  <div className={styles.filterOptions}>
                    <button
                      type="button"
                      className={`${styles.filterChip} ${hasOrdersOnly ? styles.filterChipActive : ""}`}
                      onClick={() => setHasOrdersOnly((value) => !value)}
                    >
                      Has orders
                    </button>
                    <button
                      type="button"
                      className={`${styles.filterChip} ${threePlusOrdersOnly ? styles.filterChipActive : ""}`}
                      onClick={() => setThreePlusOrdersOnly((value) => !value)}
                    >
                      ≥3 orders
                    </button>
                  </div>
                  <div className={styles.dateFieldsRow}>
                    <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                      <label className={styles.dateFieldLabel} htmlFor="order-total-min">Min total</label>
                      <div className={`${styles.dateInputWrap} ${styles.numberInputWrap}`}>
                        <input
                          id="order-total-min"
                          aria-label="Minimum order total"
                          className={styles.dateInput}
                          min="0"
                          type="number"
                          value={orderTotalMin}
                          onChange={(e) => setOrderTotalMin(e.target.value)}
                        />
                      </div>
                    </div>
                    <div className={`${styles.dateFieldGroup} ${styles.compactFieldGroup}`}>
                      <label className={styles.dateFieldLabel} htmlFor="order-total-max">Max total</label>
                      <div className={`${styles.dateInputWrap} ${styles.numberInputWrap}`}>
                        <input
                          id="order-total-max"
                          aria-label="Maximum order total"
                          className={styles.dateInput}
                          min="0"
                          type="number"
                          value={orderTotalMax}
                          onChange={(e) => setOrderTotalMax(e.target.value)}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Quality</span>
                </div>
                <div className={styles.filterGroupBody}>
                  <div className={styles.qualityRangeHeader}>
                    <span className={styles.qualityRangeValue}>{qualityMin}</span>
                    <span className={styles.qualityRangeDash}>–</span>
                    <span className={styles.qualityRangeValue}>{qualityMax}</span>
                  </div>
                  <div className={styles.qualitySliderStack}>
                    <div className={styles.qualitySliderRail} aria-hidden="true" />
                    <div
                      className={styles.qualitySliderRange}
                      aria-hidden="true"
                      style={{
                        left: `${qualityMin}%`,
                        width: `${Math.max(qualityMax - qualityMin, 0)}%`,
                      }}
                    />
                    <input
                      className={`${styles.rangeInput} ${styles.rangeInputMin}`}
                      type="range"
                      min="0"
                      max="100"
                      value={qualityMin}
                      onChange={(e) => {
                        const next = Number(e.target.value);
                        setQualityMin(Math.min(next, qualityMax));
                      }}
                    />
                    <input
                      className={`${styles.rangeInput} ${styles.rangeInputMax}`}
                      type="range"
                      min="0"
                      max="100"
                      value={qualityMax}
                      onChange={(e) => {
                        const next = Number(e.target.value);
                        setQualityMax(Math.max(next, qualityMin));
                      }}
                    />
                  </div>
                </div>
              </div>
            </div>

            <div className={styles.filterRowStack}>
              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Relations</span>
                </div>
                <div className={styles.filterGroupBody}>
                  <div className={styles.filterOptions}>
                    {(["any", "has", "none"] as const).map((value) => (
                      <button
                        key={value}
                        type="button"
                        className={`${styles.filterChip} ${relationFilter === value ? styles.filterChipActive : ""}`}
                        onClick={() => setRelationFilter(value)}
                      >
                        {value === "any" ? "Any" : value === "has" ? "Has" : "None"}
                      </button>
                    ))}
                  </div>
                  <span className={styles.filterSubLabel}>Types</span>
                  <div className={`${styles.filterOptions} ${styles.filterOptionsScrollable}`}>
                    {relationTypeOptions.map((option) => {
                      const checked = relationTypeFilter.includes(option.key);
                      return (
                        <button
                          key={option.key}
                          type="button"
                          className={`${styles.filterChip} ${checked ? styles.filterChipActive : ""}`}
                          onClick={() =>
                            setRelationTypeFilter((value) =>
                              checked
                                ? value.filter((key) => key !== option.key)
                                : [...value, option.key]
                            )
                          }
                        >
                          {option.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Entity</span>
                </div>
                <div className={styles.filterOptions}>
                  {MOCK_ENTITIES.map((entity) => (
                    <button
                      key={entity.entity_key}
                      className={`${styles.filterChip} ${entityFilter.includes(entity.entity_key) ? styles.filterChipActive : ""}`}
                      onClick={() =>
                        setEntityFilter((value) =>
                          value.includes(entity.entity_key)
                            ? value.filter((key) => key !== entity.entity_key)
                            : [...value, entity.entity_key]
                        )
                      }
                    >
                      {entity.display_name ?? entity.entity_key}
                    </button>
                  ))}
                </div>
              </div>

              <div className={styles.filterGroup}>
                <div className={styles.filterGroupHeader}>
                  <span className={styles.filterGroupLabel}>Data source</span>
                </div>
                <div className={styles.filterGroupBody}>
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
                            setSourceFilter(
                              checked
                                ? sourceFilter.filter((key) => key !== source.source_key)
                                : [...sourceFilter, source.source_key]
                            )
                          }
                        >
                          {source.display_name}
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
        {/* ── Count bar + Table ─────────────────────────────────── */}
        <div className={styles.tableSection}>
          <PaginationBar
            showing={`Showing ${filtered.length === 0 ? 0 : pageStart + 1}–${pageEnd} of ${filtered.length < ALL_PERSONS.length ? `${filtered.length} filtered` : TOTAL.toLocaleString()} profiles`}
            page={safePage} totalPages={totalPages}
            pageSize={pageSize} selectedCount={selected.size}
            onPrev={() => setPage(safePage - 1)} onNext={() => setPage(safePage + 1)}
            onPageSize={(n) => { setPageSize(n); setPage(0); }}
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
                {(["Name", "NRIC", "Phone", "Date of Birth", "Email", "Address", "Entity", "Relations", "Orders"] as const).map((h, i) => (
                  <th key={h} className={styles.th}>
                    {h}
                    <div className={styles.resizeHandle} onMouseDown={(e) => startColResize(i + 1, e)} />
                  </th>
                ))}
                <th className={`${styles.th} ${styles.thSticky} ${styles.stickyQuality}`}>
                  Completeness
                  <div className={styles.resizeHandle} onMouseDown={(e) => startColResize(10, e)} />
                </th>
                <th className={`${styles.th} ${styles.thSticky} ${styles.stickyGraph}`}>Graph</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 ? (
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
            showing={`Showing ${filtered.length === 0 ? 0 : pageStart + 1}–${pageEnd} of ${filtered.length < ALL_PERSONS.length ? `${filtered.length} filtered` : TOTAL.toLocaleString()} profiles`}
            page={safePage} totalPages={totalPages}
            pageSize={pageSize} selectedCount={selected.size}
            onPrev={() => setPage(safePage - 1)} onNext={() => setPage(safePage + 1)}
            onPageSize={(n) => { setPageSize(n); setPage(0); }}
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
