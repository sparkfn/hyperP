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
  if (!t) return null as unknown as string;
  return t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function SkeletonCard(): ReactElement {
  return (
    <div className={styles.card}>
      <div className={styles.cardLeft}>
        <span className={styles.skeleton} style={{ width: 160, height: 15, display: "block", marginBottom: 6 }} />
        <span className={styles.skeleton} style={{ width: 100, height: 11, display: "block" }} />
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <span className={styles.skeleton} style={{ width: 70, height: 18, display: "block", borderRadius: 4 }} />
          <span className={styles.skeleton} style={{ width: 36, height: 18, display: "block", borderRadius: 4 }} />
        </div>
      </div>
      <div className={styles.cardRight}>
        {[60, 60, 80].map((w, i) => (
          <div key={i} className={styles.stat}>
            <span className={styles.skeleton} style={{ width: 20, height: 16, display: "block" }} />
            <span className={styles.skeleton} style={{ width: w, height: 11, display: "block", marginTop: 2 }} />
          </div>
        ))}
      </div>
    </div>
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
      <div className={styles.header}>
        <span className={styles.title}>Entities</span>
        {!loading && <span className={styles.count}>{entities.length}</span>}
      </div>

      {error && <p className={styles.errorMsg}>{error}</p>}

      <div className={styles.list}>
        {loading && [0,1,2,3,4].map((i) => <SkeletonCard key={i} />)}

        {!loading && entities.length === 0 && !error && (
          <div className={styles.empty}>No entities found.</div>
        )}

        {!loading && entities.map((e) => {
          const typeLabel = entityTypeLabel(e.entity_type);
          return (
            <Link
              key={e.entity_key}
              href={`/persons?entity=${encodeURIComponent(e.entity_key)}`}
              className={styles.card}
            >
              {/* Left: identity */}
              <div className={styles.cardLeft}>
                <div className={styles.entityName}>{e.display_name ?? "—"}</div>
                <div className={styles.entityKey}>{e.entity_key}</div>
                <div className={styles.badges}>
                  {typeLabel && <span className={styles.typePill}>{typeLabel}</span>}
                  {e.country_code && <span className={styles.countryBadge}>{e.country_code.toUpperCase()}</span>}
                  <span className={e.is_active ? styles.badgeActive : styles.badgeInactive}>
                    {e.is_active ? "Active" : "Inactive"}
                  </span>
                </div>
              </div>

              {/* Right: stats */}
              <div className={styles.cardRight}>
                <div className={styles.stat}>
                  <span className={styles.statValue}>{e.person_count.toLocaleString()}</span>
                  <span className={styles.statLabel}>Persons</span>
                </div>
                <div className={styles.stat}>
                  <span className={styles.statValue}>{e.source_record_count.toLocaleString()}</span>
                  <span className={styles.statLabel}>Records</span>
                </div>
                {e.active_review_cases > 0 && (
                  <div className={styles.stat}>
                    <span className={styles.warnValue}>{e.active_review_cases}</span>
                    <span className={styles.statLabel}>Review cases</span>
                  </div>
                )}
                <div className={styles.stat}>
                  <span className={styles.statValue}>{relativeTime(e.last_ingested_at)}</span>
                  <span className={styles.statLabel}>Last ingested</span>
                </div>
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
