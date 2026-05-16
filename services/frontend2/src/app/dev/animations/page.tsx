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
    <svg width="200" height="200" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>
      {/* dashed guide track — appears before stamp */}
      <line x1="50" y1="14" x2="50" y2="55" stroke="currentColor" strokeWidth="0.8" strokeDasharray="3 2">
        <animate attributeName="opacity" values="0;0.22;0.22;0;0" dur="3s" repeatCount="indefinite" keyTimes="0;0.12;0.42;0.5;1" />
      </line>

      {/* field node at center */}
      <circle cx="50" cy="62" r="9" fill="currentColor">
        <animate attributeName="opacity" values="0.3;0.3;0.85;0.85;0.3" dur="3s" repeatCount="indefinite" keyTimes="0;0.4;0.5;0.75;1" />
      </circle>
      {/* ambient dashed ring — "undecided / auto" state */}
      <circle cx="50" cy="62" r="17" fill="none" stroke="currentColor" strokeWidth="0.7" strokeDasharray="3 2">
        <animate attributeName="opacity" values="0.22;0.22;0;0;0.22" dur="3s" repeatCount="indefinite" keyTimes="0;0.38;0.5;0.65;1" />
        <animate attributeName="r" values="17;17;24;24;17" dur="3s" repeatCount="indefinite" keyTimes="0;0.38;0.5;0.65;1" />
      </circle>

      {/* motion trail dot 1 */}
      <circle cx="50" cy="28" r="2.5" fill="currentColor">
        <animate attributeName="cy" values="28;28;42;56;56" dur="3s" repeatCount="indefinite" keyTimes="0;0.18;0.32;0.46;1" />
        <animate attributeName="opacity" values="0;0.35;0.35;0;0" dur="3s" repeatCount="indefinite" keyTimes="0;0.18;0.36;0.46;1" />
      </circle>
      {/* motion trail dot 2 */}
      <circle cx="50" cy="20" r="1.8" fill="currentColor">
        <animate attributeName="cy" values="20;20;34;50;50" dur="3s" repeatCount="indefinite" keyTimes="0;0.22;0.36;0.46;1" />
        <animate attributeName="opacity" values="0;0.2;0.2;0;0" dur="3s" repeatCount="indefinite" keyTimes="0;0.22;0.4;0.46;1" />
      </circle>

      {/* override node — descends, stamps, retreats */}
      <circle cx="50" cy="14" r="7" fill="currentColor">
        <animate attributeName="cy" values="14;14;53;46;46;14" dur="3s" repeatCount="indefinite" keyTimes="0;0.1;0.44;0.52;0.72;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="r" values="7;7;9;7;7;7" dur="3s" repeatCount="indefinite" keyTimes="0;0.1;0.46;0.54;0.72;1" />
        <animate attributeName="opacity" values="0.2;0.88;0.88;0.88;0.2;0.2" dur="3s" repeatCount="indefinite" keyTimes="0;0.14;0.46;0.72;0.86;1" />
      </circle>

      {/* impact ring at field on stamp */}
      <circle cx="50" cy="62" r="9" fill="none" stroke="currentColor" strokeWidth="1.5">
        <animate attributeName="r" values="9;9;20;20;9" dur="3s" repeatCount="indefinite" keyTimes="0;0.47;0.58;0.65;1" />
        <animate attributeName="opacity" values="0;0.7;0;0;0" dur="3s" repeatCount="indefinite" keyTimes="0;0.47;0.6;0.65;1" />
      </circle>

      {/* pin dot — stays on field after stamp */}
      <circle cx="50" cy="62" r="0" fill="currentColor">
        <animate attributeName="r" values="0;0;3.5;3.5;0" dur="3s" repeatCount="indefinite" keyTimes="0;0.5;0.56;0.78;1" />
        <animate attributeName="opacity" values="0;0;0.9;0.9;0" dur="3s" repeatCount="indefinite" keyTimes="0;0.5;0.56;0.78;1" />
      </circle>
    </svg>
  );
}

function GraphAnimation(): ReactElement {
  return (
    <svg width="160" height="160" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>
      <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4" opacity="0.25" />
      <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3" opacity="0.18" />
      <circle cx="50" cy="50" r="7" fill="currentColor" opacity="0.55" />
      <g><animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" /><line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" /><circle cx="72" cy="50" r="5" fill="currentColor" opacity="0.65" /></g>
      <g><animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" /><line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" /><circle cx="72" cy="50" r="4" fill="currentColor" opacity="0.5" /></g>
      <g><animateTransform attributeName="transform" type="rotate" from="60 50 50" to="-300 50 50" dur="5s" repeatCount="indefinite" /><line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" /><circle cx="86" cy="50" r="4" fill="currentColor" opacity="0.55" /></g>
      <g><animateTransform attributeName="transform" type="rotate" from="180 50 50" to="-180 50 50" dur="5s" repeatCount="indefinite" /><line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" /><circle cx="86" cy="50" r="3.5" fill="currentColor" opacity="0.45" /></g>
      <g><animateTransform attributeName="transform" type="rotate" from="300 50 50" to="-60 50 50" dur="5s" repeatCount="indefinite" /><line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" /><circle cx="86" cy="50" r="3" fill="currentColor" opacity="0.4" /></g>
    </svg>
  );
}

export default function AnimationsPage(): ReactElement {
  return (
    <div style={{ minHeight: "100vh", background: "#0f172a", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 48, padding: 40, fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ color: "#94a3b8", fontSize: 13, letterSpacing: "0.1em", textTransform: "uppercase", margin: 0 }}>Animation Preview</h1>

      <div style={{ display: "flex", gap: 64, flexWrap: "wrap", justifyContent: "center" }}>
        {([
          { label: "Graph loading", el: <GraphAnimation /> },
          { label: "Merge loading", el: <MergeAnimation /> },
          { label: "Override loading", el: <OverrideAnimation /> },
        ] as const).map(({ label, el }) => (
          <div key={label} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
            <div style={{ width: 240, height: 240, background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 16, display: "flex", alignItems: "center", justifyContent: "center" }}>
              {el}
            </div>
            <p style={{ color: "#64748b", fontSize: 12, margin: 0 }}>{label}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
