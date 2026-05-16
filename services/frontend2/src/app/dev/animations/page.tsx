"use client";
import { notFound } from "next/navigation";
import type { ReactElement } from "react";

if (process.env.NODE_ENV !== "development") notFound();

function MergeAnimation(): ReactElement {
  return (
    <svg width="240" height="120" viewBox="0 0 120 60" style={{ color: "var(--text-secondary, #6b7280)" }}>
      <line x1="18" y1="30" x2="102" y2="30" stroke="currentColor" strokeWidth="1" strokeDasharray="4 3">
        <animate attributeName="opacity" values="0.25;0;0;0.25" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.38;0.6;1" />
      </line>
      <circle cx="18" cy="30" r="9" fill="currentColor">
        <animate attributeName="cx" values="18;60;60;18" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.4;0.6;1" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="opacity" values="0.55;0;0;0.55" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.4;0.6;1" />
      </circle>
      <circle cx="102" cy="30" r="9" fill="currentColor">
        <animate attributeName="cx" values="102;60;60;102" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.4;0.6;1" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="opacity" values="0.55;0;0;0.55" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.4;0.6;1" />
      </circle>
      <circle cx="60" cy="30" r="0" fill="currentColor">
        <animate attributeName="r" values="0;13;13;0" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.42;0.58;1" />
        <animate attributeName="opacity" values="0;0.85;0.85;0" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.42;0.58;1" />
      </circle>
      <circle cx="60" cy="30" r="0" fill="none" stroke="currentColor" strokeWidth="1.5">
        <animate attributeName="r" values="0;22;22;0" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.5;0.56;1" />
        <animate attributeName="opacity" values="0;0.3;0;0" dur="2.4s" repeatCount="indefinite" keyTimes="0;0.5;0.56;1" />
      </circle>
    </svg>
  );
}

function OverrideAnimation(): ReactElement {
  return (
    <svg width="240" height="140" viewBox="0 0 120 70" style={{ color: "var(--text-secondary, #6b7280)" }}>
      <text x="10" y="16" fontSize="7" fill="currentColor" opacity="0.3" fontFamily="monospace">old</text>
      <rect x="10" y="20" width="100" height="9" rx="3" fill="currentColor">
        <animate attributeName="opacity" values="0.2;0.05;0.2" dur="2s" repeatCount="indefinite" keyTimes="0;0.5;1" />
      </rect>
      <text x="10" y="46" fontSize="7" fill="currentColor" opacity="0.55" fontFamily="monospace">new</text>
      <rect x="10" y="50" width="0" height="9" rx="3" fill="currentColor" opacity="0.75">
        <animate attributeName="width" values="0;100;100;0" dur="2s" repeatCount="indefinite" keyTimes="0;0.4;0.7;1" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.6 0 1 1" />
        <animate attributeName="opacity" values="0.75;0.75;0;0" dur="2s" repeatCount="indefinite" keyTimes="0;0.4;0.7;1" />
      </rect>
      <rect x="10" y="48" width="2" height="13" rx="1" fill="currentColor" opacity="0">
        <animate attributeName="x" values="10;108;10" dur="2s" repeatCount="indefinite" keyTimes="0;0.4;1" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0" />
        <animate attributeName="opacity" values="0.9;0.9;0" dur="2s" repeatCount="indefinite" keyTimes="0;0.38;0.42" />
      </rect>
      <line x1="60" y1="48" x2="60" y2="32" stroke="currentColor" strokeWidth="1">
        <animate attributeName="opacity" values="0;0.35;0.35;0" dur="2s" repeatCount="indefinite" keyTimes="0;0.42;0.65;1" />
      </line>
      <polygon points="56,34 60,28 64,34" fill="currentColor">
        <animate attributeName="opacity" values="0;0.5;0.5;0" dur="2s" repeatCount="indefinite" keyTimes="0;0.42;0.65;1" />
      </polygon>
    </svg>
  );
}

export default function AnimationsPage(): ReactElement {
  return (
    <div style={{ minHeight: "100vh", background: "#0f172a", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 48, padding: 40, fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ color: "#94a3b8", fontSize: 13, letterSpacing: "0.1em", textTransform: "uppercase", margin: 0 }}>Animation Preview</h1>

      <div style={{ display: "flex", gap: 64, flexWrap: "wrap", justifyContent: "center" }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
          <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 16, padding: 40, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <MergeAnimation />
          </div>
          <p style={{ color: "#64748b", fontSize: 12, margin: 0 }}>Merge loading</p>
        </div>

        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
          <div style={{ background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 16, padding: 40, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <OverrideAnimation />
          </div>
          <p style={{ color: "#64748b", fontSize: 12, margin: 0 }}>Override loading</p>
        </div>
      </div>
    </div>
  );
}
