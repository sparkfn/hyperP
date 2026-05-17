"use client";

import { useEffect, useState, type ReactElement } from "react";
import Link from "next/link";

import { bffFetchEnvelope } from "@/lib/api-client";
import type { EntitySummary } from "@/lib/api-types";
import { formatDate } from "@/lib/display";
import styles from "./entities.module.css";

// ── Helpers ──────────────────────────────────────────────────────────────────

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(iso);
}

function entityTypeLabel(t: string | null): string {
  if (!t) return "—";
  return t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Skeleton ─────────────────────────────────────────────────────────────────

function SkeletonRow(): ReactElement {
  return (
    <tr className={styles.skeletonRow}>
      {[140, 80, 60, 50, 60, 50, 60, 80, 50].map((w, i) => (
        <td key={i}><span className={styles.skeleton} style={{ width: w, height: 12, display: "inline-block" }} /></td>
      ))}
    </tr>
  );
}

// ── KPI tile ─────────────────────────────────────────────────────────────────

function KpiTile({ label, value, sub, accent, warn }: {
  label: string; value: string | number; sub?: string; accent?: boolean; warn?: boolean;
}): ReactElement {
  return (
    <div className={`${styles.kpi} ${accent ? styles.kpiAccent : ""} ${warn ? styles.kpiWarn : ""}`}>
      <span className={styles.kpiLabel}>{label}</span>
      <span className={styles.kpiValue}>{value}</span>
      {sub && <span className={styles.kpiSub}>{sub}</span>}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

function EntitiesContent(): ReactElement {
  const [entities, setEntities] = useState<EntitySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    void bffFetchEnvelope<EntitySummary[]>("/bff/entities")
      .then((res) => setEntities(res.data ?? []))
      .catch(() => setError("Failed to load entities."))
      .finally(() => setLoading(false));
  }, []);

  const total = entities.length;
  const activeCount = entities.filter((e) => e.is_active).length;
  const totalReview = entities.reduce((s, e) => s + e.active_review_cases, 0);

  return (
    <div className={styles.page}>
      {/* ── Header ── */}
      <div className={styles.header}>
        <div>
          <div className={styles.breadcrumb}>
            <span className={styles.breadcrumbMuted}>Admin</span>
            <span className={styles.breadcrumbSep}>/</span>
            <span className={styles.breadcrumbCurrent}>Entities</span>
          </div>
          <h1 className={styles.title}>Entities</h1>
        </div>
      </div>

      {/* ── KPI strip ── */}
      <div className={styles.kpiStrip}>
        <KpiTile label="Total entities" value={loading ? "—" : total} accent />
        <KpiTile label="Active" value={loading ? "—" : activeCount} sub={loading ? undefined : `${total - activeCount} inactive`} />
        <KpiTile
          label="Pending review"
          value={loading ? "—" : totalReview}
          sub={totalReview > 0 ? "Requires attention" : "All clear"}
          warn={totalReview > 0}
        />
        <KpiTile label="Total persons" value={loading ? "—" : entities.reduce((s, e) => s + e.person_count, 0)} />
      </div>

      {/* ── Error ── */}
      {error && <p className={styles.errorMsg}>{error}</p>}

      {/* ── Table ── */}
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th className={styles.th}>Entity</th>
              <th className={styles.th}>Type</th>
              <th className={styles.th}>Country</th>
              <th className={styles.th}>Status</th>
              <th className={styles.th} style={{ textAlign: "right" }}>Persons</th>
              <th className={styles.th} style={{ textAlign: "right" }}>Records</th>
              <th className={styles.th} style={{ textAlign: "right" }}>Review cases</th>
              <th className={styles.th}>Last ingested</th>
              <th className={styles.th}></th>
            </tr>
          </thead>
          <tbody>
            {loading && [0, 1, 2, 3, 4].map((i) => <SkeletonRow key={i} />)}
            {!loading && entities.length === 0 && !error && (
              <tr>
                <td colSpan={9} className={styles.emptyCell}>No entities found.</td>
              </tr>
            )}
            {!loading && entities.map((e) => (
              <tr key={e.entity_key} className={styles.row}>
                {/* Entity name + key */}
                <td className={styles.td}>
                  <div className={styles.nameCell}>
                    <span className={styles.name}>{e.display_name ?? <span className={styles.muted}>—</span>}</span>
                    <span className={styles.entityKey}>{e.entity_key}</span>
                  </div>
                </td>

                {/* Type */}
                <td className={styles.td}>
                  {e.entity_type ? (
                    <span className={styles.typePill}>{entityTypeLabel(e.entity_type)}</span>
                  ) : (
                    <span className={styles.muted}>—</span>
                  )}
                </td>

                {/* Country */}
                <td className={styles.td}>
                  {e.country_code ? (
                    <span className={styles.countryBadge}>{e.country_code.toUpperCase()}</span>
                  ) : (
                    <span className={styles.muted}>—</span>
                  )}
                </td>

                {/* Status */}
                <td className={styles.td}>
                  <span className={e.is_active ? styles.badgeActive : styles.badgeInactive}>
                    {e.is_active ? "Active" : "Inactive"}
                  </span>
                </td>

                {/* Persons */}
                <td className={`${styles.td} ${styles.numCell}`}>
                  {e.person_count.toLocaleString()}
                </td>

                {/* Source records */}
                <td className={`${styles.td} ${styles.numCell}`}>
                  {e.source_record_count.toLocaleString()}
                </td>

                {/* Review cases */}
                <td className={`${styles.td} ${styles.numCell}`}>
                  {e.active_review_cases > 0 ? (
                    <span className={styles.warnCount}>{e.active_review_cases}</span>
                  ) : (
                    <span className={styles.muted}>—</span>
                  )}
                </td>

                {/* Last ingested */}
                <td className={styles.td}>
                  <span className={styles.dateCell} title={e.last_ingested_at ?? undefined}>
                    {relativeTime(e.last_ingested_at)}
                  </span>
                </td>

                {/* View link */}
                <td className={`${styles.td} ${styles.actionCell}`}>
                  <Link href={`/persons?entity=${encodeURIComponent(e.entity_key)}`} className={styles.viewLink}>
                    Persons →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function EntitiesPage(): ReactElement {
  return <EntitiesContent />;
}
