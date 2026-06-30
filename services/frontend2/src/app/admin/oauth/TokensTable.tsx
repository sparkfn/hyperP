"use client";

import { useState, type ReactElement } from "react";

import type { OAuthAccessToken } from "@/lib/api-types-ops";
import { relativeTime } from "@/lib/display";
import styles from "../admin.module.css";

function epochToIso(seconds: number): string {
  return new Date(seconds * 1000).toISOString();
}

const MAX_VISIBLE_TOKENS = 5;

export function TokensTable({
  tokens,
  onRevoke,
}: {
  tokens: OAuthAccessToken[];
  onRevoke: (jti: string) => Promise<void>;
}): ReactElement {
  const [busyJti, setBusyJti] = useState<string | null>(null);

  async function handleRevoke(jti: string): Promise<void> {
    setBusyJti(jti);
    try {
      await onRevoke(jti);
    } finally {
      setBusyJti(null);
    }
  }

  if (tokens.length === 0) {
    return <p className={styles.tokenEmpty}>No active tokens.</p>;
  }

  const visible = tokens.slice(0, MAX_VISIBLE_TOKENS);

  return (
    <>
    <table className={styles.tokenTable}>
      <thead>
        <tr>
          <th>Issued</th>
          <th>Expires</th>
          <th>Last used</th>
          <th>IP</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {visible.map((t) => (
          <tr key={t.jti}>
            <td>{relativeTime(epochToIso(t.issued_at))}</td>
            <td>{relativeTime(epochToIso(t.expires_at))}</td>
            <td>{t.last_used_at !== null ? relativeTime(epochToIso(t.last_used_at)) : "—"}</td>
            <td>{t.last_used_ip ?? "—"}</td>
            <td>
              <button
                type="button"
                className={styles.actionBtnDanger}
                disabled={busyJti === t.jti}
                onClick={() => void handleRevoke(t.jti)}
              >
                {busyJti === t.jti ? "Revoking…" : "Revoke"}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
    {tokens.length > MAX_VISIBLE_TOKENS && (
      <p className={styles.tokenHint}>
        Showing {MAX_VISIBLE_TOKENS} most recent of {tokens.length} active tokens.
      </p>
    )}
    </>
  );
}
