"use client";

import { type ReactElement } from "react";
import Link from "next/link";

import type { EntitySummary } from "@/lib/api-types";
import { entityColor, getInitials, relativeTime } from "@/lib/display";
import { useBffList } from "@/lib/useBffList";
import styles from "./entities.module.css";
function entityTypeLabel(t: string | null): string | null {
  if (!t) return null;
  return t.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function SkeletonCard(): ReactElement {
  return (
    <div className={styles.card}>
      <div className={styles.cardTop}>
        <span className={styles.skeleton} style={{ width: 48, height: 48, borderRadius: 12, display: "block" }} />
        <div style={{ flex: 1 }}>
          <span className={styles.skeleton} style={{ width: "60%", height: 14, display: "block", marginBottom: 6 }} />
          <span className={styles.skeleton} style={{ width: "40%", height: 11, display: "block" }} />
        </div>
      </div>
      <div className={styles.cardDivider} />
      <div className={styles.cardStats}>
        {[0,1,2].map((i) => (
          <div key={i} className={styles.stat}>
            <span className={styles.skeleton} style={{ width: 28, height: 16, display: "block" }} />
            <span className={styles.skeleton} style={{ width: 50, height: 10, display: "block", marginTop: 3 }} />
          </div>
        ))}
      </div>
    </div>
  );
}

export default function EntitiesPage(): ReactElement {
  const { data: entities, loading, error } = useBffList<EntitySummary>("/bff/entities", "Failed to load entities.");

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <span className={styles.title}>Entities</span>
        {!loading && <span className={styles.count}>{entities.length}</span>}
      </div>

      {error && <p className={styles.errorMsg}>{error}</p>}

      <div className={styles.grid}>
        {loading && [0,1,2,3,4,5].map((i) => <SkeletonCard key={i} />)}

        {!loading && entities.length === 0 && !error && (
          <div className={styles.empty}>No entities found.</div>
        )}

        {!loading && entities.map((e) => {
          const color = entityColor(e.entity_key);
          const init = getInitials(e.display_name, e.entity_key);
          const typeLabel = entityTypeLabel(e.entity_type);
          return (
            <Link key={e.entity_key} href={`/persons?entity=${encodeURIComponent(e.entity_key)}`} className={styles.card}>
              {/* Logo + name */}
              <div className={styles.cardTop}>
                <div className={styles.avatar} style={{ background: color }}>{init}</div>
                <div className={styles.cardMeta}>
                  <div className={styles.entityName}>{e.display_name ?? e.entity_key}</div>
                  <div className={styles.entityKey}>{e.entity_key}</div>
                </div>
              </div>

              {/* Badges row */}
              <div className={styles.badges}>
                {typeLabel && <span className={styles.typePill}>{typeLabel}</span>}
                {e.country_code && <span className={styles.countryBadge}>{e.country_code.toUpperCase()}</span>}
                <span className={e.is_active ? styles.badgeActive : styles.badgeInactive}>
                  {e.is_active ? "Active" : "Inactive"}
                </span>
              </div>

              <div className={styles.cardDivider} />

              {/* Stats */}
              <div className={styles.cardStats}>
                <div className={styles.stat}>
                  <span className={styles.statValue}>{e.person_count.toLocaleString()}</span>
                  <span className={styles.statLabel}>Persons</span>
                </div>
                <div className={styles.stat}>
                  <span className={styles.statValue}>{e.source_record_count.toLocaleString()}</span>
                  <span className={styles.statLabel}>Records</span>
                </div>
                <div className={styles.stat}>
                  {e.active_review_cases > 0
                    ? <span className={styles.warnValue}>{e.active_review_cases}</span>
                    : <span className={styles.statValue}>—</span>}
                  <span className={styles.statLabel}>Review</span>
                </div>
                <div className={styles.stat}>
                  <span className={styles.statValue} style={{ fontSize: 11 }}>{relativeTime(e.last_ingested_at)}</span>
                  <span className={styles.statLabel}>Last sync</span>
                </div>
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
