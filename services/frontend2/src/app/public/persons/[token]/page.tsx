import "server-only";
import type { ReactElement } from "react";
import { notFound } from "next/navigation";
import { apiFetch } from "@/lib/api-server";
import type { Person, PersonConnection, SalesOrder } from "@/lib/api-types";
import type { PersonIdentifier } from "@/lib/api-types-person";
import { UpstreamError } from "@/lib/api-server";
import styles from "./public-person.module.css";

interface PageProps {
  params: Promise<{ token: string }>;
}

function formatDob(dob: string | null): string {
  if (!dob) return "—";
  const d = new Date(`${dob}T00:00:00`);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function formatAmount(amount: number | null, currency: string | null): string {
  if (amount == null) return "—";
  return new Intl.NumberFormat("en-SG", {
    style: "currency",
    currency: currency ?? "SGD",
    minimumFractionDigits: 2,
  }).format(amount);
}

function maskNric(nric: string | null): string {
  if (!nric) return "—";
  return nric.length >= 4 ? `****${nric.slice(-4)}` : "****";
}

function identifierLabel(type: string): string {
  const map: Record<string, string> = {
    phone: "Phone",
    email: "Email",
    nric: "NRIC",
    passport: "Passport",
    uen: "UEN",
    singpass: "Singpass",
  };
  return map[type] ?? type;
}

export default async function PublicPersonPage({ params }: PageProps): Promise<ReactElement> {
  const { token } = await params;

  let person: Person;
  let identifiers: PersonIdentifier[];
  let connections: PersonConnection[];
  let sales: SalesOrder[];

  try {
    const opts = { authToken: null as null };
    [person, identifiers, connections, sales] = await Promise.all([
      apiFetch<Person>(`/public/persons/${token}`, opts).then((r) => r.data),
      apiFetch<PersonIdentifier[]>(`/public/persons/${token}/identifiers`, opts).then((r) => r.data).catch(() => []),
      apiFetch<PersonConnection[]>(`/public/persons/${token}/connections`, opts).then((r) => r.data).catch(() => []),
      apiFetch<SalesOrder[]>(`/public/persons/${token}/sales`, opts).then((r) => r.data).catch(() => []),
    ]);
  } catch (e) {
    if (e instanceof UpstreamError && e.status === 404) notFound();
    throw e;
  }

  const activeIdentifiers = identifiers.filter((id) => id.is_active);
  const phones = activeIdentifiers.filter((id) => id.identifier_type === "phone");
  const emails = activeIdentifiers.filter((id) => id.identifier_type === "email");
  const otherIds = activeIdentifiers.filter((id) => id.identifier_type !== "phone" && id.identifier_type !== "email");

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <div className={styles.wordmark}>HyperP</div>
          <div className={styles.headerMeta}>Shared profile · read-only</div>
        </div>
      </header>

      <main className={styles.main}>
        {/* ── Person header ── */}
        <div className={styles.personHeader}>
          <div className={styles.avatar}>
            {(person.preferred_full_name ?? "?").charAt(0).toUpperCase()}
          </div>
          <div className={styles.personInfo}>
            <h1 className={styles.personName}>{person.preferred_full_name ?? "Unknown"}</h1>
            <div className={styles.personMeta}>
              {person.preferred_dob && <span>{formatDob(person.preferred_dob)}</span>}
              {person.preferred_nric && <span>{maskNric(person.preferred_nric)}</span>}
              <span className={`${styles.statusBadge} ${styles[`status_${person.status}`]}`}>
                {person.status}
              </span>
            </div>
          </div>
        </div>

        {/* ── Contact info ── */}
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>Contact information</h2>
          <div className={styles.fieldGrid}>
            <div className={styles.fieldGroup}>
              <div className={styles.fieldLabel}>Phone</div>
              {phones.length > 0
                ? phones.map((id) => <div key={id.normalized_value} className={styles.fieldValue}>{id.normalized_value}</div>)
                : <div className={styles.fieldValue}>—</div>}
            </div>
            <div className={styles.fieldGroup}>
              <div className={styles.fieldLabel}>Email</div>
              {emails.length > 0
                ? emails.map((id) => <div key={id.normalized_value} className={styles.fieldValue}>{id.normalized_value}</div>)
                : <div className={styles.fieldValue}>—</div>}
            </div>
            {person.preferred_address && (
              <div className={`${styles.fieldGroup} ${styles.fieldGroupFull}`}>
                <div className={styles.fieldLabel}>Address</div>
                <div className={styles.fieldValue}>{person.preferred_address.normalized_full ?? "—"}</div>
              </div>
            )}
            {otherIds.map((id) => (
              <div key={`${id.identifier_type}-${id.normalized_value}`} className={styles.fieldGroup}>
                <div className={styles.fieldLabel}>{identifierLabel(id.identifier_type)}</div>
                <div className={styles.fieldValue}>{id.normalized_value}</div>
              </div>
            ))}
          </div>
        </section>

        {/* ── Connections ── */}
        {connections.length > 0 && (
          <section className={styles.section}>
            <h2 className={styles.sectionTitle}>Connections <span className={styles.sectionCount}>{connections.length}</span></h2>
            <div className={styles.connectionList}>
              {connections.map((conn) => {
                const rel = conn.knows_relationships[0];
                const label = rel?.relationship_label ?? rel?.relationship_category ?? null;
                return (
                  <div key={conn.person_id} className={styles.connectionItem}>
                    <div className={styles.connectionName}>{conn.preferred_full_name ?? conn.person_id}</div>
                    {label && <div className={styles.connectionRel}>{label}</div>}
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* ── Sales ── */}
        {sales.length > 0 && (
          <section className={styles.section}>
            <h2 className={styles.sectionTitle}>Order history <span className={styles.sectionCount}>{sales.length}</span></h2>
            <table className={styles.salesTable}>
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Date</th>
                  <th>Items</th>
                  <th className={styles.colRight}>Amount</th>
                </tr>
              </thead>
              <tbody>
                {sales.map((order) => (
                  <tr key={order.order_no ?? order.source_order_id}>
                    <td className={styles.orderNo}>{order.order_no ?? order.source_order_id ?? "—"}</td>
                    <td>{formatDate(order.order_date)}</td>
                    <td>{order.line_items[0]?.product?.display_name ?? "—"}{order.line_items.length > 1 ? ` +${order.line_items.length - 1}` : ""}</td>
                    <td className={styles.colRight}>{formatAmount(order.total_amount, order.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}

        <footer className={styles.footer}>
          Generated by HyperP · {new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })}
        </footer>
      </main>
    </div>
  );
}
