"use client";

import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { BffError, bffFetch } from "@/lib/api-client";
import type { ReviewCaseDetail } from "@/lib/api-types-ops";
import { useSetLoading } from "@/lib/LoadingContext";
import styles from "../review.module.css";
import { ReviewCaseDetailContent } from "./ReviewCaseDetailModal";

export default function ReviewCaseDetailPage(): ReactElement {
  const params = useParams<{ reviewCaseId?: string | string[] }>();
  const reviewCaseId = typeof params.reviewCaseId === "string" ? params.reviewCaseId : "";
  const [detail, setDetail] = useState<ReviewCaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const setGlobalLoading = useSetLoading();
  const loadId = useMemo(() => `review-case-${reviewCaseId}`, [reviewCaseId]);

  const loadDetail = useCallback((): void => {
    if (reviewCaseId.length === 0) {
      setError("Review case id is missing.");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    setGlobalLoading(loadId, true);
    bffFetch<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}`)
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
