"use client";

import { useState, type ReactElement, type FormEvent } from "react";

import { BffError, bffFetch } from "@/lib/api-client";
import type { EntitySummary } from "@/lib/api-types";
import type { UserResponse, UserBulkCreateRow, UserBulkCreateResponse, UserBulkCreateResult } from "@/lib/api-types-ops";
import type { Role } from "@/lib/permissions";
import { useBffList } from "@/lib/useBffList";
import styles from "../admin.module.css";

const ROLES: Role[] = ["admin", "employee", "first_time"];

const ROLE_COLOR: Record<string, string> = {
  admin:      "#3b82f6",
  employee:   "#22c55e",
  first_time: "#94a3b8",
};

// ── Skeleton ──────────────────────────────────────────────────────────────────

function SkeletonCard(): ReactElement {
  return (
    <div className={styles.card}>
      <div className={styles.cardRow}>
        <span className={styles.skeleton} style={{ width: 36, height: 36, borderRadius: "50%", display: "block", flexShrink: 0 }} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 6 }}>
          <span className={styles.skeleton} style={{ width: "55%", height: 13, display: "block" }} />
          <span className={styles.skeleton} style={{ width: "40%", height: 10, display: "block" }} />
        </div>
      </div>
    </div>
  );
}

// ── User Card ─────────────────────────────────────────────────────────────────

function EditUserModal({ u, entities, onClose }: { u: UserResponse; entities: EntitySummary[]; onClose: () => void }): ReactElement {
  const [role, setRole] = useState<Role>(u.role as Role);
  const [entityKey, setEntityKey] = useState(u.entity_key ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave(e: FormEvent): Promise<void> {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await bffFetch(`/bff/admin/users/${encodeURIComponent(u.email)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, entity_key: entityKey || null }),
      });
      onClose();
    } catch (err) {
      setError(err instanceof BffError ? err.message : "Failed to update.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={styles.modalOverlay} onClick={onClose}>
      <form className={styles.modal} style={{ maxWidth: 380 }} onSubmit={(e) => void handleSave(e)} onClick={(e) => e.stopPropagation()}>
        <div className={styles.formHeader}>
          <span className={styles.formTitle}>Edit User</span>
          <button type="button" className={styles.formClose} onClick={onClose}>×</button>
        </div>
        <div className={styles.formField}>
          <label className={styles.formLabel}>Email</label>
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>{u.email}</span>
        </div>
        <div className={styles.formField}>
          <label className={styles.formLabel}>Role</label>
          <select className={styles.formInput} value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
        <div className={styles.formField}>
          <label className={styles.formLabel}>Entity</label>
          <select className={styles.formInput} value={entityKey} onChange={(e) => setEntityKey(e.target.value)}>
            <option value="">— none —</option>
            {entities.map((e) => (
              <option key={e.entity_key} value={e.entity_key}>{e.display_name ?? e.entity_key}</option>
            ))}
          </select>
        </div>
        {error && <p className={styles.formError}>{error}</p>}
        <div className={styles.formActions}>
          <button type="button" className={styles.formCancel} onClick={onClose}>Cancel</button>
          <button type="submit" className={styles.formSubmit} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

function UserCard({ u, entities, onRefresh }: { u: UserResponse; entities: EntitySummary[]; onRefresh: () => void }): ReactElement {
  const initials = (u.display_name ?? u.email).split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  const [editing, setEditing] = useState(false);
  const color = ROLE_COLOR[u.role] ?? "#94a3b8";

  function handleClose(): void {
    setEditing(false);
    onRefresh();
  }

  return (
    <>
      <div className={styles.card}>
        <div className={styles.cardRow}>
          <div className={styles.avatarUser} style={{ background: `${color}20`, color }}>
            {initials}
          </div>
          <div className={styles.cardInfo}>
            <span className={styles.cardName}>{u.display_name ?? u.email}</span>
            <span className={styles.cardKey}>{u.email}</span>
          </div>
          <span className={styles.roleBadge} style={{ color, background: `${color}18` }}>{u.role}</span>
        </div>
        <div className={styles.cardActions}>
          {u.entity_key && <span className={styles.metaItem}>{u.entity_key}</span>}
          <button type="button" className={styles.actionBtnEdit} style={{ marginLeft: "auto" }} onClick={() => setEditing(true)}>Edit</button>
        </div>
      </div>
      {editing && <EditUserModal u={u} entities={entities} onClose={handleClose} />}
    </>
  );
}

// ── Bulk Add Modal ────────────────────────────────────────────────────────────

interface RowDraft { email: string; role: Role; entity_key: string }

const EMPTY_ROW = (): RowDraft => ({ email: "", role: "employee", entity_key: "" });

function BulkAddModal({ onClose }: { onClose: () => void }): ReactElement {
  const { data: entities } = useBffList<EntitySummary>("/bff/entities");
  const [rows, setRows] = useState<RowDraft[]>([EMPTY_ROW()]);
  const [submitting, setSubmitting] = useState(false);
  const [results, setResults] = useState<UserBulkCreateResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  function updateRow(i: number, field: keyof RowDraft, value: string): void {
    setRows((prev) => prev.map((r, idx) => idx === i ? { ...r, [field]: value } : r));
  }

  function addRow(): void {
    setRows((prev) => [...prev, EMPTY_ROW()]);
  }

  function removeRow(i: number): void {
    setRows((prev) => prev.filter((_, idx) => idx !== i));
  }

  async function handleSubmit(e: FormEvent): Promise<void> {
    e.preventDefault();
    const valid = rows.filter((r) => r.email.trim());
    if (valid.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const body: { users: UserBulkCreateRow[] } = {
        users: valid.map((r) => ({
          email: r.email.trim(),
          role: r.role,
          entity_key: r.entity_key.trim() || null,
        })),
      };
      const res = await bffFetch<UserBulkCreateResponse>("/bff/admin/users/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setResults(res.results);
    } catch (err) {
      setError(err instanceof BffError ? err.message : "Failed to add users.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.modalOverlay} onClick={onClose}>
      <div className={styles.modal} style={{ maxWidth: 600 }} onClick={(e) => e.stopPropagation()}>
        <div className={styles.formHeader}>
          <span className={styles.formTitle}>{results ? "Results" : "Bulk Add Users"}</span>
          <button type="button" className={styles.formClose} onClick={onClose}>×</button>
        </div>

        {/* Results view */}
        {results ? (
          <>
            <div className={styles.bulkResults}>
              {results.map((r) => (
                <div key={r.email} className={`${styles.bulkResultRow} ${r.status === "error" ? styles.bulkResultError : styles.bulkResultOk}`}>
                  <span className={styles.bulkResultIcon}>{r.status === "error" ? "✕" : "✓"}</span>
                  <span className={styles.bulkResultEmail}>{r.email}</span>
                  {r.status === "error" && <span className={styles.bulkResultMsg}>{r.message}</span>}
                  {r.status === "created" && r.user && <span className={styles.bulkResultMsg}>{r.user.role}</span>}
                </div>
              ))}
            </div>
            <div className={styles.formActions}>
              <button type="button" className={styles.formSubmit} onClick={onClose}>Done</button>
            </div>
          </>
        ) : (
          /* Form */
          <form onSubmit={(e) => void handleSubmit(e)} style={{ display: "contents" }}>
            {/* Header row */}
            <div className={styles.bulkHeader}>
              <span style={{ flex: 1 }}>Email</span>
              <span style={{ width: 120 }}>Role</span>
              <span style={{ width: 120 }}>Entity</span>
              <span style={{ width: 28 }} />
            </div>

            {/* Data rows */}
            <div className={styles.bulkRows}>
              {rows.map((row, i) => (
                <div key={i} className={styles.bulkRow}>
                  <input
                    className={`${styles.formInput} ${styles.bulkInput}`}
                    type="email"
                    placeholder="user@example.com"
                    value={row.email}
                    onChange={(e) => updateRow(i, "email", e.target.value)}
                    required={i === 0}
                  />
                  <select
                    className={`${styles.formInput} ${styles.bulkSelect}`}
                    value={row.role}
                    onChange={(e) => updateRow(i, "role", e.target.value)}
                  >
                    {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                  <select
                    className={`${styles.formInput} ${styles.bulkSelect}`}
                    value={row.entity_key}
                    onChange={(e) => updateRow(i, "entity_key", e.target.value)}
                  >
                    <option value="">— none —</option>
                    {entities.map((e) => (
                      <option key={e.entity_key} value={e.entity_key}>
                        {e.display_name ?? e.entity_key}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className={styles.bulkRemoveBtn}
                    onClick={() => removeRow(i)}
                    disabled={rows.length === 1}
                    aria-label="Remove row"
                  >×</button>
                </div>
              ))}
            </div>

            <button type="button" className={styles.bulkAddRowBtn} onClick={addRow}>
              + Add row
            </button>

            {error && <p className={styles.formError}>{error}</p>}

            <div className={styles.formActions}>
              <button type="button" className={styles.formCancel} onClick={onClose}>Cancel</button>
              <button type="submit" className={styles.formSubmit} disabled={submitting || rows.every((r) => !r.email.trim())}>
                {submitting ? "Adding…" : `Add ${rows.filter((r) => r.email.trim()).length} user${rows.filter((r) => r.email.trim()).length !== 1 ? "s" : ""}`}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function UsersPage(): ReactElement {
  const { data: users, loading, refresh } = useBffList<UserResponse>("/bff/admin/users", "Failed to load users.");
  const { data: entities } = useBffList<EntitySummary>("/bff/entities");
  const [modalOpen, setModalOpen] = useState(false);

  function handleClose(): void {
    setModalOpen(false);
    refresh();
  }

  return (
    <div className={styles.simplePage}>
      <div className={styles.pageHeader}>
        <h1 className={styles.title}>Users</h1>
        {!loading && <span className={styles.count}>{users.length}</span>}
        <div style={{ marginLeft: "auto" }}>
          <button type="button" className={styles.createBtn} onClick={() => setModalOpen(true)}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Add Users
          </button>
        </div>
      </div>

      <div className={styles.grid}>
        {loading && [0, 1, 2].map((i) => <SkeletonCard key={i} />)}
        {!loading && users.length === 0 && <p className={styles.empty}>No users found.</p>}
        {!loading && users.map((u) => <UserCard key={u.email} u={u} entities={entities} onRefresh={refresh} />)}
      </div>

      {modalOpen && <BulkAddModal onClose={handleClose} />}
    </div>
  );
}
