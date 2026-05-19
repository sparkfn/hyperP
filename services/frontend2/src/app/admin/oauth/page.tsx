"use client";

import { useState, type ReactElement, type FormEvent } from "react";

import { BffError, bffFetch, bffFetchEnvelope } from "@/lib/api-client";
import type { OAuthClient, OAuthClientCreated, CreateOAuthClientRequest } from "@/lib/api-types-ops";
import { OAUTH_CLIENT_SCOPES } from "@/lib/api-types-ops";
import { relativeTime } from "@/lib/display";
import { useBffList } from "@/lib/useBffList";
import styles from "../admin.module.css";

// ── Skeleton ──────────────────────────────────────────────────────────────────

function SkeletonCard(): ReactElement {
  return (
    <div className={styles.card}>
      <div className={styles.cardRow}>
        <span className={styles.skeleton} style={{ width: 36, height: 36, borderRadius: 8, display: "block", flexShrink: 0 }} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 6 }}>
          <span className={styles.skeleton} style={{ width: "55%", height: 13, display: "block" }} />
          <span className={styles.skeleton} style={{ width: "35%", height: 10, display: "block" }} />
        </div>
      </div>
    </div>
  );
}

// ── OAuth Client Card ─────────────────────────────────────────────────────────

function OAuthClientCard({ c, onRefresh }: { c: OAuthClient; onRefresh: () => void }): ReactElement {
  const isDisabled = !!c.disabled_at;
  const activeSecrets = c.secrets.filter((s) => !s.revoked_at).length;
  const [busy, setBusy] = useState(false);

  async function handleDelete(): Promise<void> {
    if (!confirm(`Delete client "${c.name}"? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await bffFetchEnvelope(`/bff/admin/oauth-clients/${encodeURIComponent(c.client_id)}`, { method: "DELETE" });
      onRefresh();
    } catch { /* ignore */ } finally { setBusy(false); }
  }

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
      <div className={styles.cardActions}>
        <button type="button" className={styles.actionBtnDanger} disabled={busy} onClick={() => void handleDelete()}>
          {busy ? "Deleting…" : "Delete"}
        </button>
      </div>
    </div>
  );
}

// ── Create Modal ──────────────────────────────────────────────────────────────

function CreateModal({ onClose }: { onClose: () => void }): ReactElement {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [expireDays, setExpireDays] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<OAuthClientCreated | null>(null);
  const [copied, setCopied] = useState(false);

  function toggleScope(scope: string): void {
    setScopes((s) => s.includes(scope) ? s.filter((x) => x !== scope) : [...s, scope]);
  }

  async function handleSubmit(e: FormEvent): Promise<void> {
    e.preventDefault();
    if (!name.trim() || scopes.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const body: CreateOAuthClientRequest = {
        name: name.trim(),
        entity_key: null,
        scopes,
        secret_expires_in_days: expireDays ? parseInt(expireDays, 10) : null,
      };
      const res = await bffFetch<OAuthClientCreated>("/bff/admin/oauth-clients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setCreated(res);
    } catch (err) {
      setError(err instanceof BffError ? err.message : "Failed to create client.");
    } finally {
      setSubmitting(false);
    }
  }

  function copySecret(): void {
    if (!created) return;
    void navigator.clipboard.writeText(created.client_secret)
      .then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); })
      .catch(() => undefined);
  }

  return (
    <div className={styles.modalOverlay} onClick={onClose}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()}>

        {/* Header */}
        <div className={styles.formHeader}>
          <span className={styles.formTitle}>
            {created ? "Client Created" : "New OAuth Client"}
          </span>
          <button type="button" className={styles.formClose} onClick={onClose}>×</button>
        </div>

        {/* Secret view */}
        {created ? (
          <>
            <div className={styles.secretNotice}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
              Save the secret now — it will not be shown again.
            </div>
            <div className={styles.formField}>
              <label className={styles.formLabel}>Client ID</label>
              <code className={styles.secretCode}>{created.client_id}</code>
            </div>
            <div className={styles.formField}>
              <label className={styles.formLabel}>Client Secret</label>
              <code className={styles.secretCode}>{created.client_secret}</code>
            </div>
            <div className={styles.formActions}>
              <button type="button" className={styles.copyBtn} onClick={copySecret}>
                {copied ? "Copied!" : "Copy secret"}
              </button>
              <button type="button" className={styles.formSubmit} onClick={onClose}>
                Done
              </button>
            </div>
          </>
        ) : (
          /* Form */
          <form onSubmit={(e) => void handleSubmit(e)} style={{ display: "contents" }}>
            <div className={styles.formField}>
              <label className={styles.formLabel}>Name <span className={styles.required}>*</span></label>
              <input className={styles.formInput} type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Data Pipeline" required autoFocus />
            </div>
            <div className={styles.formField}>
              <label className={styles.formLabel}>Expires in <span className={styles.optional}>(days, optional)</span></label>
              <input className={styles.formInput} type="number" min="1" value={expireDays} onChange={(e) => setExpireDays(e.target.value)} placeholder="Never" />
            </div>
            <div className={styles.formField}>
              <label className={styles.formLabel}>Scopes <span className={styles.required}>*</span></label>
              <div className={styles.scopeGrid}>
                {OAUTH_CLIENT_SCOPES.map((sc) => (
                  <button key={sc} type="button"
                    className={`${styles.scopeChip} ${scopes.includes(sc) ? styles.scopeChipActive : ""}`}
                    onClick={() => toggleScope(sc)}>
                    {sc}
                  </button>
                ))}
              </div>
            </div>
            {error && <p className={styles.formError}>{error}</p>}
            <div className={styles.formActions}>
              <button type="button" className={styles.formCancel} onClick={onClose}>Cancel</button>
              <button type="submit" className={styles.formSubmit} disabled={submitting || !name.trim() || scopes.length === 0}>
                {submitting ? "Creating…" : "Create Client"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function OAuthPage(): ReactElement {
  const { data: clients, loading, refresh } = useBffList<OAuthClient>("/bff/admin/oauth-clients", "Failed to load OAuth clients.");
  const [modalOpen, setModalOpen] = useState(false);

  function handleClose(): void {
    setModalOpen(false);
    refresh();
  }

  return (
    <div className={styles.simplePage}>
      <div className={styles.pageHeader}>
        <h1 className={styles.title}>OAuth Clients</h1>
        {!loading && <span className={styles.count}>{clients.length}</span>}
        <div style={{ marginLeft: "auto" }}>
          <button type="button" className={styles.createBtn} onClick={() => setModalOpen(true)}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            New OAuth Client
          </button>
        </div>
      </div>

      <div className={styles.grid}>
        {loading && [0, 1].map((i) => <SkeletonCard key={i} />)}
        {!loading && clients.length === 0 && <p className={styles.empty}>No OAuth clients.</p>}
        {!loading && clients.map((c) => <OAuthClientCard key={c.client_id} c={c} onRefresh={refresh} />)}
      </div>

      {modalOpen && <CreateModal onClose={handleClose} />}
    </div>
  );
}
