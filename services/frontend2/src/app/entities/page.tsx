"use client";

import { useEffect, useState, type ReactElement } from "react";
import Link from "next/link";

import { bffFetchEnvelope } from "@/lib/api-client";
import type { EntitySummary } from "@/lib/api-types";
import { formatDate } from "@/lib/display";
import styles from "./entities.module.css";

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

function SkeletonRow(): ReactElement {
  return (
    <tr className={styles.skeletonRow}>
      {[160, 90, 60, 60, 70, 70, 80, 90].map((w, i) => (
        <td key={i}><span className={styles.skeleton} style={{ width: w, height: 12, display: "inline-block" }} /></td>
      ))}
    </tr>
  );
}

export default function EntitiesPage(): ReactElement {
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

  return (
    <div className={styles.page}>
      {/* Breadcrumb + title */}
      <div className={styles.header}>
        <span className={styles.title}>Entities</span>
        {!loading && <span className={styles.count}>{entities.length}</span>}
      </div>

      {error && <p className={styles.errorMsg}>{error}</p>}

      {/* Table */}
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th className={styles.th}>Entity</th>
              <th className={styles.th}>Type</th>
              <th className={styles.th}>Country</th>
              <th className={styles.th}>Status</th>
              <th className={styles.thNum}>Persons</th>
              <th className={styles.thNum}>Records</th>
              <th className={styles.thNum}>Review cases</th>
              <th className={styles.th}>Last ingested</th>
            </tr>
          </thead>
          <tbody>
            {loading && [0,1,2,3,4].map((i) => <SkeletonRow key={i} />)}
            {!loading && entities.length === 0 && !error && (
              <tr><td colSpan={8} className={styles.emptyCell}>No entities found.</td></tr>
            )}
            {!loading && entities.map((e) => (
              <tr key={e.entity_key} className={styles.row}>
                <td className={styles.td}>
                  <div className={styles.nameCell}>
                    <Link href={`/persons?entity=${encodeURIComponent(e.entity_key)}`} className={styles.nameLink}>
                      {e.display_name ?? <span className={styles.muted}>—</span>}
                    </Link>
                    <span className={styles.entityKey}>{e.entity_key}</span>
                  </div>
                </td>
                <td className={styles.td}>
                  {e.entity_type
                    ? <span className={styles.typePill}>{entityTypeLabel(e.entity_type)}</span>
                    : <span className={styles.muted}>—</span>}
                </td>
                <td className={styles.td}>
                  {e.country_code
                    ? <span className={styles.countryBadge}>{e.country_code.toUpperCase()}</span>
                    : <span className={styles.muted}>—</span>}
                </td>
                <td className={styles.td}>
                  <span className={e.is_active ? styles.badgeActive : styles.badgeInactive}>
                    {e.is_active ? "Active" : "Inactive"}
                  </span>
                </td>
                <td className={styles.tdNum}>{e.person_count.toLocaleString()}</td>
                <td className={styles.tdNum}>{e.source_record_count.toLocaleString()}</td>
                <td className={styles.tdNum}>
                  {e.active_review_cases > 0
                    ? <span className={styles.warnCount}>{e.active_review_cases}</span>
                    : <span className={styles.muted}>—</span>}
                </td>
                <td className={styles.td}>
                  <span className={styles.dateCell} title={e.last_ingested_at ?? undefined}>
                    {relativeTime(e.last_ingested_at)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
