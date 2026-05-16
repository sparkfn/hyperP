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
  const spline = "0 0 0 0;0.4 0 0.2 1;0.4 0 0.2 1;0.4 0 0.2 1;0.4 0 0.2 1";
  const spiralX = "83;83;18;28;50;83";
  const spiralY = "14;14;30;68;28;14";
  const spiralT = "0;0.06;0.3;0.52;0.64;1";

  return (
    <svg width="200" height="200" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>

      {/* ── orbit group: 3 field nodes + edges rotating together ── */}
      <g>
        <animateTransform attributeName="transform" type="rotate"
          from="0 50 52" to="360 50 52" dur="9.6s" repeatCount="indefinite" />
        {/* edges */}
        <line x1="50" y1="52" x2="50" y2="28" stroke="currentColor" strokeWidth="0.8" opacity="0.2" />
        <line x1="50" y1="52" x2="71" y2="64" stroke="currentColor" strokeWidth="0.8" opacity="0.2" />
        <line x1="50" y1="52" x2="29" y2="64" stroke="currentColor" strokeWidth="0.8" opacity="0.2" />
        {/* target field node (top) — fades on impact */}
        <circle cx="50" cy="28" r="4.5" fill="currentColor">
          <animate attributeName="opacity" values="0.38;0.38;0.05;0.05;0.38" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.58;0.65;0.82;1" />
          <animate attributeName="r"       values="4.5;4.5;1;0;4.5"          dur="3.2s" repeatCount="indefinite" keyTimes="0;0.58;0.65;0.68;1" />
        </circle>
        {/* other field nodes — gentle breathing */}
        <circle cx="71" cy="64" r="4.5" fill="currentColor">
          <animate attributeName="opacity" values="0.28;0.4;0.28;0.28" dur="3.1s" repeatCount="indefinite" />
        </circle>
        <circle cx="29" cy="64" r="4.5" fill="currentColor">
          <animate attributeName="opacity" values="0.28;0.28;0.4;0.28" dur="2.9s" repeatCount="indefinite" begin="0.8s" />
        </circle>
      </g>

      {/* ── person node ── */}
      <circle cx="50" cy="52" r="9" fill="currentColor">
        <animate attributeName="opacity" values="0.55;0.7;0.55;0.65;0.55" dur="2.1s" repeatCount="indefinite" />
        <animate attributeName="r"       values="9;10;9;9.5;9"             dur="2.1s" repeatCount="indefinite" />
      </circle>

      {/* ── target edge flash on impact ── */}
      <line x1="50" y1="52" x2="50" y2="28" stroke="currentColor">
        <animate attributeName="strokeWidth" values="0.8;0.8;3;3;0.8" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.62;0.68;0.82;1" />
        <animate attributeName="opacity"     values="0.0;0.0;0.9;0.9;0.0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.62;0.68;0.82;1" />
      </line>

      {/* ── source node spiraling in ── */}
      <circle fill="currentColor">
        <animate attributeName="cx" values={spiralX} dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} calcMode="spline" keySplines={spline} />
        <animate attributeName="cy" values={spiralY} dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} calcMode="spline" keySplines={spline} />
        <animate attributeName="r"       values="5.5;5.5;6;6.5;7;5.5"    dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} />
        <animate attributeName="opacity" values="0;0.85;0.9;0.92;0.95;0" dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} />
      </circle>

      {/* ── trail dot 1 ── */}
      <circle fill="currentColor">
        <animate attributeName="cx" values={spiralX} dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} calcMode="spline" keySplines={spline} begin="0.07s" />
        <animate attributeName="cy" values={spiralY} dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} calcMode="spline" keySplines={spline} begin="0.07s" />
        <animate attributeName="r"       values="3;3;3.5;4;2;0"       dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} begin="0.07s" />
        <animate attributeName="opacity" values="0;0.4;0.45;0.48;0;0" dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} begin="0.07s" />
      </circle>

      {/* ── trail dot 2 ── */}
      <circle fill="currentColor">
        <animate attributeName="cx" values={spiralX} dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} calcMode="spline" keySplines={spline} begin="0.13s" />
        <animate attributeName="cy" values={spiralY} dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} calcMode="spline" keySplines={spline} begin="0.13s" />
        <animate attributeName="r"       values="1.8;1.8;2.2;2.5;1;0"  dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} begin="0.13s" />
        <animate attributeName="opacity" values="0;0.25;0.28;0.3;0;0"  dur="3.2s" repeatCount="indefinite" keyTimes={spiralT} begin="0.13s" />
      </circle>

      {/* ── impact rings ── */}
      <circle cx="50" cy="28" fill="none" stroke="currentColor" strokeWidth="1.5">
        <animate attributeName="r"       values="0;0;6;22;0;0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.63;0.65;0.78;0.84;1" />
        <animate attributeName="opacity" values="0;0;0.8;0;0;0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.63;0.65;0.78;0.84;1" />
      </circle>
      <circle cx="50" cy="28" fill="none" stroke="currentColor" strokeWidth="0.8">
        <animate attributeName="r"       values="0;0;6;30;0;0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.65;0.67;0.82;0.88;1" />
        <animate attributeName="opacity" values="0;0;0.4;0;0;0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.65;0.67;0.82;0.88;1" />
      </circle>

      {/* ── lock dot ── */}
      <circle cx="50" cy="28" fill="currentColor">
        <animate attributeName="r"       values="0;0;4.5;4.5;0;0"    dur="3.2s" repeatCount="indefinite" keyTimes="0;0.66;0.7;0.84;0.9;1" />
        <animate attributeName="opacity" values="0;0;0.88;0.88;0;0"  dur="3.2s" repeatCount="indefinite" keyTimes="0;0.66;0.7;0.84;0.9;1" />
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
