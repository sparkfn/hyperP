"use client";

import type { ReactElement } from "react";

import type { SourceSystemInfo } from "@/lib/api-types-ops";
import { entityColor, relativeTime } from "@/lib/display";
import { useBffList } from "@/lib/useBffList";
import styles from "./ingestion.module.css";

function SourceCard({ s }: { s: SourceSystemInfo }): ReactElement {
  const color    = entityColor(s.source_key);
  const initials = (s.display_name ?? s.source_key).slice(0, 2).toUpperCase();

  return (
    <div className={styles.card}>
      <div className={styles.cardTop}>
        <div className={styles.avatar} style={{ background: color }}>{initials}</div>
        <div className={styles.cardInfo}>
          <div className={styles.cardName}>{s.display_name ?? s.source_key}</div>
          <div className={styles.cardKey}>{s.source_key}</div>
        </div>
        <span
          className={styles.statusBadge}
          style={{ color: s.is_active ? "#22c55e" : "#94a3b8", background: s.is_active ? "#22c55e18" : "#94a3b818" }}
        >
          {s.is_active ? "active" : "inactive"}
        </span>
      </div>

      {(s.system_type ?? s.entity_key) && (
        <div className={styles.cardMeta}>
          {s.system_type && <span className={styles.metaTag}>{s.system_type}</span>}
          {s.entity_key  && <span className={styles.metaTag}>{s.entity_key}</span>}
          {s.updated_at  && <span className={styles.metaUpdated}>Updated {relativeTime(s.updated_at)}</span>}
        </div>
      )}
    </div>
  );
}

export default function IngestionPage(): ReactElement {
  const { data: sources, loading, error } = useBffList<SourceSystemInfo>("/bff/source-systems", "Failed to load source systems.");

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <h1 className={styles.title}>Sources</h1>
        {!loading && <span className={styles.count}>{sources.length}</span>}
      </div>
      <p className={styles.subtitle}>Connected source systems and their status.</p>

      {error && <p className={styles.error}>{error}</p>}

      {loading ? (
        <div className={styles.grid}>
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className={styles.cardSkeleton}>
              <div className={styles.skeletonTop}>
                <div className={styles.skeletonAvatar} />
                <div className={styles.skeletonLines}>
                  <div className={styles.skeletonLine} style={{ width: "60%" }} />
                  <div className={styles.skeletonLine} style={{ width: "40%" }} />
                </div>
              </div>
              <div className={styles.skeletonBtn} />
            </div>
          ))}
        </div>
      ) : sources.length === 0 ? (
        <div className={styles.empty}>No source systems registered.</div>
      ) : (
        <div className={styles.grid}>
          {sources.map((s) => <SourceCard key={s.source_key} s={s} />)}
        </div>
      )}
    </div>
  );
}
