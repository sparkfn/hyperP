"use client";

import { type ReactElement } from "react";

import type { SourceSystemInfo, OAuthClient } from "@/lib/api-types-ops";
import { relativeTime } from "@/lib/display";
import { useBffList } from "@/lib/useBffList";
import styles from "./admin.module.css";

// ── Skeleton ──────────────────────────────────────────────────────────────────

function SkeletonCard({ lines = 2 }: { lines?: number }): ReactElement {
  return (
    <div className={styles.card}>
      <div className={styles.cardRow}>
        <span className={styles.skeleton} style={{ width: 36, height: 36, borderRadius: 8, display: "block", flexShrink: 0 }} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 6 }}>
          <span className={styles.skeleton} style={{ width: "55%", height: 13, display: "block" }} />
          <span className={styles.skeleton} style={{ width: "35%", height: 10, display: "block" }} />
        </div>
      </div>
      {lines > 1 && (
        <div className={styles.cardMeta}>
          {Array.from({ length: lines - 1 }, (_, i) => (
            <span key={i} className={styles.skeleton} style={{ width: `${50 + i * 15}px`, height: 10, display: "inline-block" }} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Source Systems ────────────────────────────────────────────────────────────

const SOURCE_PALETTE: Record<string, { fg: string; bg: string }> = {
  fundbox:   { fg: "#1e40af", bg: "#dbeafe" },
  eko:       { fg: "#166534", bg: "#dcfce7" },
  speedzone: { fg: "#9f1239", bg: "#ffe4e6" },
  bitrix:    { fg: "#92400e", bg: "#fef3c7" },
};
function sourceStyle(key: string): { fg: string; bg: string } {
  const lower = key.toLowerCase();
  for (const [k, v] of Object.entries(SOURCE_PALETTE)) {
    if (lower.includes(k)) return v;
  }
  return { fg: "#4361ee", bg: "color-mix(in srgb, var(--bg-card) 60%, var(--bg-app))" };
}

function SourceSystemCard({ s }: { s: SourceSystemInfo }): ReactElement {
  const label = s.display_name ?? s.source_key;
  const abbr = label.slice(0, 2).toUpperCase();
  const fieldCount = Object.keys(s.field_trust).length;
  return (
    <div className={styles.card}>
      <div className={styles.cardRow}>
        <div className={styles.avatar} style={{ background: sourceStyle(s.source_key).bg, color: sourceStyle(s.source_key).fg }}>
          {abbr}
        </div>
        <div className={styles.cardInfo}>
          <span className={styles.cardName}>{label}</span>
          <span className={styles.cardKey}>{s.source_key}</span>
        </div>
        <span className={s.is_active ? styles.dotActive : styles.dotInactive} />
      </div>
      <div className={styles.cardMeta}>
        {s.system_type && <span className={styles.pill}>{s.system_type}</span>}
        {s.entity_key && <span className={styles.pill}>{s.entity_key}</span>}
        <span className={styles.metaItem}>{fieldCount} field{fieldCount !== 1 ? "s" : ""} trusted</span>
        <span className={styles.metaItem}>Updated {relativeTime(s.updated_at)}</span>
      </div>
    </div>
  );
}

// ── OAuth Clients ─────────────────────────────────────────────────────────────

function OAuthClientCard({ c }: { c: OAuthClient }): ReactElement {
  const isDisabled = !!c.disabled_at;
  const activeSecrets = c.secrets.filter((s) => !s.revoked_at).length;
  return (
    <div className={styles.card}>
      <div className={styles.cardRow}>
        <div className={styles.avatarKey}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>
          </svg>
        </div>
        <div className={styles.cardInfo}>
          <span className={styles.cardName}>{c.name}</span>
          <span className={styles.cardKey}>{c.client_id}</span>
        </div>
        <span className={isDisabled ? styles.dotInactive : styles.dotActive} />
      </div>
      <div className={styles.cardMeta}>
        {c.scopes.map((sc) => <span key={sc} className={styles.scopePill}>{sc}</span>)}
        <span className={styles.metaItem}>{activeSecrets} active secret{activeSecrets !== 1 ? "s" : ""}</span>
        {c.last_used_at && <span className={styles.metaItem}>Used {relativeTime(c.last_used_at)}</span>}
        <span className={styles.metaItem}>by {c.created_by}</span>
      </div>
    </div>
  );
}

// ── Section ───────────────────────────────────────────────────────────────────

function Section({ title, icon, children }: { title: string; icon: ReactElement; children: ReactElement }): ReactElement {
  return (
    <section className={styles.section}>
      <div className={styles.sectionHeader}>
        {icon}
        <h2 className={styles.sectionTitle}>{title}</h2>
      </div>
      {children}
    </section>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AdminPage(): ReactElement {
  const { data: sources, loading: sourcesLoading } = useBffList<SourceSystemInfo>("/bff/source-systems", "Failed to load source systems.");
  const { data: clients, loading: clientsLoading } = useBffList<OAuthClient>("/bff/admin/oauth-clients", "Failed to load OAuth clients.");

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Admin</h1>

      <Section
        title="Source Systems"
        icon={<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>}
      >
        <div className={styles.grid}>
          {sourcesLoading && [0,1,2].map((i) => <SkeletonCard key={i} lines={2} />)}
          {!sourcesLoading && sources.length === 0 && <p className={styles.empty}>No source systems configured.</p>}
          {!sourcesLoading && sources.map((s) => <SourceSystemCard key={s.source_key} s={s} />)}
        </div>
      </Section>

      <Section
        title="OAuth Clients"
        icon={<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>}
      >
        <div className={styles.grid}>
          {clientsLoading && [0,1].map((i) => <SkeletonCard key={i} lines={2} />)}
          {!clientsLoading && clients.length === 0 && <p className={styles.empty}>No OAuth clients.</p>}
          {!clientsLoading && clients.map((c) => <OAuthClientCard key={c.client_id} c={c} />)}
        </div>
      </Section>
    </div>
  );
}
