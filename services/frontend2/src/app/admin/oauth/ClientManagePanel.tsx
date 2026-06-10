"use client";

import { useCallback, useEffect, useState, type ReactElement } from "react";

import { BffError, bffFetch, bffFetchEnvelope } from "@/lib/api-client";
import type { OAuthAccessToken, OAuthClient, RotateSecretResponse } from "@/lib/api-types-ops";
import styles from "../admin.module.css";
import { TokensTable } from "./TokensTable";
import { ttlMinutesToSeconds, ttlSecondsToMinutes } from "./ttl";

export function ClientManagePanel({
  c,
  onRefresh,
}: {
  c: OAuthClient;
  onRefresh: () => void;
}): ReactElement {
  const clientPath = `/bff/admin/oauth-clients/${encodeURIComponent(c.client_id)}`;

  // ── Token lifetime ──
  const [ttlMinutes, setTtlMinutes] = useState(String(ttlSecondsToMinutes(c.access_token_ttl_seconds)));
  const [savingTtl, setSavingTtl] = useState(false);
  const [ttlError, setTtlError] = useState<string | null>(null);

  async function saveTtl(): Promise<void> {
    const seconds = ttlMinutesToSeconds(ttlMinutes);
    setSavingTtl(true);
    setTtlError(null);
    try {
      await bffFetchEnvelope(clientPath, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_token_ttl_seconds: seconds }),
      });
      onRefresh();
    } catch (err) {
      setTtlError(err instanceof BffError ? err.message : "Failed to save.");
    } finally {
      setSavingTtl(false);
    }
  }

  // ── Rotate secret ──
  const [rotating, setRotating] = useState(false);
  const [rotated, setRotated] = useState<RotateSecretResponse | null>(null);
  const [rotateError, setRotateError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function rotateSecret(): Promise<void> {
    if (!confirm("Rotating revokes the current secret and invalidates all live access tokens for this client. Continue?")) return;
    setRotating(true);
    setRotateError(null);
    try {
      const res = await bffFetch<RotateSecretResponse>(`${clientPath}/rotate-secret`, { method: "POST" });
      setRotated(res);
      // Do NOT call onRefresh() here: the parent list refresh flips useBffList
      // back into its loading state, which unmounts this card (and panel) and
      // would wipe the one-time secret before it can be copied. The card shows
      // nothing that goes stale on rotation; the list refreshes on next load.
      void loadTokens();
    } catch (err) {
      setRotateError(err instanceof BffError ? err.message : "Failed to rotate secret.");
    } finally {
      setRotating(false);
    }
  }

  function copySecret(): void {
    if (!rotated) return;
    void navigator.clipboard.writeText(rotated.client_secret)
      .then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); })
      .catch(() => undefined);
  }

  // ── Tokens ──
  const [tokens, setTokens] = useState<OAuthAccessToken[]>([]);
  const [tokensLoading, setTokensLoading] = useState(true);
  const [tokensError, setTokensError] = useState<string | null>(null);

  const loadTokens = useCallback(async (): Promise<void> => {
    setTokensLoading(true);
    setTokensError(null);
    try {
      const res = await bffFetch<OAuthAccessToken[]>(`${clientPath}/tokens`);
      setTokens(res);
    } catch (err) {
      setTokensError(err instanceof BffError ? err.message : "Failed to load tokens.");
    } finally {
      setTokensLoading(false);
    }
  }, [clientPath]);

  useEffect(() => {
    void loadTokens();
  }, [loadTokens]);

  const revokeToken = useCallback(async (jti: string): Promise<void> => {
    await bffFetchEnvelope(`${clientPath}/tokens/${encodeURIComponent(jti)}/revoke`, { method: "POST" });
    await loadTokens();
  }, [clientPath, loadTokens]);

  return (
    <div className={styles.managePanel}>
      <div className={styles.manageSection}>
        <span className={styles.manageLabel}>Token lifetime</span>
        <div className={styles.ttlRow}>
          <input
            className={styles.ttlInput}
            type="number"
            min="5"
            max="1440"
            value={ttlMinutes}
            onChange={(e) => setTtlMinutes(e.target.value)}
          />
          <span className={styles.ttlUnit}>minutes</span>
          <button type="button" className={styles.actionBtnEdit} disabled={savingTtl} onClick={() => void saveTtl()}>
            {savingTtl ? "Saving…" : "Save"}
          </button>
        </div>
        {ttlError && <p className={styles.formError}>{ttlError}</p>}
      </div>

      <div className={styles.manageSection}>
        <span className={styles.manageLabel}>Secret</span>
        {rotated ? (
          <>
            <div className={styles.secretNotice}>
              Save the secret now — it will not be shown again.
            </div>
            <code className={styles.secretCode}>{rotated.client_secret}</code>
            <div className={styles.ttlRow}>
              <button type="button" className={styles.copyBtn} onClick={copySecret}>
                {copied ? "Copied!" : "Copy secret"}
              </button>
            </div>
          </>
        ) : (
          <div className={styles.ttlRow}>
            <button type="button" className={styles.actionBtnWarn} disabled={rotating} onClick={() => void rotateSecret()}>
              {rotating ? "Rotating…" : "Rotate secret"}
            </button>
          </div>
        )}
        {rotateError && <p className={styles.formError}>{rotateError}</p>}
      </div>

      <div className={styles.manageSection}>
        <div className={styles.ttlRow}>
          <span className={styles.manageLabel}>Active tokens</span>
          <button
            type="button"
            className={styles.actionBtnEdit}
            disabled={tokensLoading}
            onClick={() => void loadTokens()}
          >
            {tokensLoading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <p className={styles.tokenHint}>Last used / IP populate after a token calls an API endpoint, not on issue.</p>
        {tokensLoading && <p className={styles.tokenEmpty}>Loading…</p>}
        {!tokensLoading && tokensError && <p className={styles.formError}>{tokensError}</p>}
        {!tokensLoading && !tokensError && <TokensTable tokens={tokens} onRevoke={revokeToken} />}
      </div>
    </div>
  );
}
