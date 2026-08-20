import React, { type ReactElement } from "react";

import styles from "./persons.module.css";

export function CrmDealCount({ count }: { count: number }): ReactElement {
  if (count === 0) {
    return <span className={styles.crmDealCountZero}>No deals</span>;
  }

  return (
    <span className={styles.crmDealValue}>
      <span className={styles.crmDealCount}>{count.toLocaleString()}</span>
      <span className={styles.crmDealLabel}>deals</span>
    </span>
  );
}
