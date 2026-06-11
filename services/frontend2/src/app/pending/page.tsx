import type { ReactElement } from "react";

import styles from "./pending.module.css";

export default function PendingPage(): ReactElement {
  return (
    <main className={styles.page}>
      <section className={styles.card}>
        <div className={styles.badge}>Account pending</div>
        <h1 className={styles.title}>Your access is waiting for approval.</h1>
        <p className={styles.description}>
          Your Google account is signed in, but it has not been assigned an active HyperP role yet. Ask an admin to approve your account, then refresh this page.
        </p>
      </section>
    </main>
  );
}
