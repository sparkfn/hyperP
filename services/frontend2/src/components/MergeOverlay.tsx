"use client";

import type { ReactElement } from "react";

import styles from "./MergeOverlay.module.css";

interface MergeOverlayProps {
  label?: string;
  fixed?: boolean;
}

export default function MergeOverlay({ label, fixed = false }: MergeOverlayProps): ReactElement {
  return (
    <div className={fixed ? styles.overlayFixed : styles.overlay} aria-label={label ?? "Processing…"} role="status">
      <svg className={styles.spinner} viewBox="0 0 100 100" overflow="visible" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        <g>
          <animateTransform attributeName="transform" type="translate" values="-23 0;0 0;0 0;22 0;22 0;-23 0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0" />
          <circle cx="28" cy="50" r="12" fill="none" stroke="currentColor" strokeWidth="0.4">
            <animate attributeName="opacity" values="0;0;0.22;0.22;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
          </circle>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="0 28 50" to="360 28 50" dur="2.4s" repeatCount="indefinite" />
            <line x1="28" y1="50" x2="40" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="40" cy="50" r="3.5" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.60;0.60;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="180 28 50" to="540 28 50" dur="2.4s" repeatCount="indefinite" />
            <line x1="28" y1="50" x2="40" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="40" cy="50" r="3" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.42;0.42;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <circle cx="28" cy="50" r="6" fill="currentColor">
            <animate attributeName="opacity" values="0;0.55;0.55;0;0;0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="translate" values="23 0;0 0;0 0;-22 0;-22 0;23 0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0" />
          <circle cx="72" cy="50" r="12" fill="none" stroke="currentColor" strokeWidth="0.4">
            <animate attributeName="opacity" values="0;0;0.22;0.22;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
          </circle>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="0 72 50" to="360 72 50" dur="2s" repeatCount="indefinite" />
            <line x1="72" y1="50" x2="84" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="84" cy="50" r="3.5" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.60;0.60;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="240 72 50" to="600 72 50" dur="2s" repeatCount="indefinite" />
            <line x1="72" y1="50" x2="84" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="84" cy="50" r="3" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.42;0.42;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="120 72 50" to="480 72 50" dur="2s" repeatCount="indefinite" />
            <line x1="72" y1="50" x2="84" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="84" cy="50" r="2.5" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.35;0.35;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <circle cx="72" cy="50" r="6" fill="currentColor">
            <animate attributeName="opacity" values="0;0.55;0.55;0;0;0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <circle cx="50" cy="50" r="0" fill="none" stroke="currentColor" strokeWidth="1.5">
          <animate attributeName="r"       values="0;0;8;38;0;0"  keyTimes="0;0.18;0.21;0.34;0.38;1" dur="8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.5;0;0;0" keyTimes="0;0.18;0.21;0.34;0.38;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="0" fill="currentColor">
          <animate attributeName="r"       values="0;0;8;8;0;0"     keyTimes="0;0.18;0.24;0.82;0.90;1" dur="8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.7;0.7;0;0" keyTimes="0;0.18;0.24;0.82;0.90;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4">
          <animate attributeName="opacity" values="0;0;0;0.25;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3">
          <animate attributeName="opacity" values="0;0;0;0.18;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7">
            <animate attributeName="opacity" values="0;0;0;0.30;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="72" cy="50" r="5" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.65;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7">
            <animate attributeName="opacity" values="0;0;0;0.30;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="72" cy="50" r="4" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.50;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="60 50 50" to="-300 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5">
            <animate attributeName="opacity" values="0;0;0;0.22;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="86" cy="50" r="4" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.55;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="-180 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5">
            <animate attributeName="opacity" values="0;0;0;0.22;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="86" cy="50" r="3.5" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.45;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="300 50 50" to="-60 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5">
            <animate attributeName="opacity" values="0;0;0;0.22;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="86" cy="50" r="3" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.40;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
      </svg>
      {label !== undefined && <div className={styles.label}>{label}</div>}
    </div>
  );
}
