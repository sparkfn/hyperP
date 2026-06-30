"use client";

import { useEffect, type ReactElement } from "react";

import styles from "./ActionToast.module.css";

export interface ToastState {
  type: "success" | "error";
  message: string;
}

interface ActionToastProps extends ToastState {
  onDismiss: () => void;
}

export default function ActionToast({ type, message, onDismiss }: ActionToastProps): ReactElement {
  useEffect(() => {
    const timer = setTimeout(onDismiss, 4000);
    return () => clearTimeout(timer);
  }, [onDismiss]);

  return (
    <div className={`${styles.toast} ${type === "success" ? styles.success : styles.error}`} role="status">
      <span className={styles.icon} aria-hidden="true">
        {type === "success" ? (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 6L9 17l-5-5" />
          </svg>
        ) : (
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
        )}
      </span>
      <span className={styles.message}>{message}</span>
      <button type="button" className={styles.dismiss} onClick={onDismiss} aria-label="Dismiss">✕</button>
    </div>
  );
}
