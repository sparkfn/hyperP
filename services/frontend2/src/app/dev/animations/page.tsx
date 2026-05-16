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
  // Like graph loading: 1 ring, center node, 2 orbiting field nodes.
  // One field node gets replaced by an incoming override node.
  // Override node approaches from outside the ring, swaps in. Loop 4s.
  return (
    <svg width="200" height="200" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>
      {/* ── orbit track ring ── */}
      <circle cx="50" cy="50" r="28" fill="none" stroke="currentColor" strokeWidth="0.4" opacity="0.25" />

      {/* ── center node ── */}
      <circle cx="50" cy="50" r="7" fill="currentColor" opacity="0.55" />

      {/* ── orbiting field node A (steady, CCW 6s) ── */}
      <g>
        <animateTransform attributeName="transform" type="rotate"
          from="180 50 50" to="-180 50 50" dur="6s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="78" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
        <circle cx="78" cy="50" r="5" fill="currentColor" opacity="0.55" />
      </g>

      {/* ── orbiting field node B (target, CW 4s) — fades when overridden ── */}
      <g>
        <animateTransform attributeName="transform" type="rotate"
          from="0 50 50" to="360 50 50" dur="4s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="78" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
        <circle cx="78" cy="50" r="5" fill="currentColor">
          {/* fades out mid-cycle, restores at end */}
          <animate attributeName="opacity" values="0.55;0.55;0.05;0.05;0.55" dur="4s" repeatCount="indefinite" keyTimes="0;0.42;0.52;0.72;1" />
          <animate attributeName="r"       values="5;5;1.5;0;5"               dur="4s" repeatCount="indefinite" keyTimes="0;0.42;0.52;0.55;1" />
        </circle>
      </g>

      {/* ── override node: approaches from outside, snaps onto ring ──
           Starts far (92,50), moves to ring radius (78,50) in the rotating frame.
           We animate in world space: from (92,50) approaching toward (78,50),
           then once on the ring, it orbits CW with field B from that point. ── */}
      <circle fill="currentColor">
        {/* Phase 1 (0→0.5): approach from outside to ring edge */}
        <animate attributeName="cx" values="92;92;78;78;92" dur="4s" repeatCount="indefinite"
          keyTimes="0;0.1;0.52;0.72;1"
          calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="cy" values="50;50;50;50;50" dur="4s" repeatCount="indefinite"
          keyTimes="0;0.1;0.52;0.72;1" />
        <animate attributeName="opacity" values="0;0.8;0.9;0.9;0"  dur="4s" repeatCount="indefinite"
          keyTimes="0;0.12;0.52;0.72;1" />
        <animate attributeName="r"       values="5;5.5;6;5;5"      dur="4s" repeatCount="indefinite"
          keyTimes="0;0.1;0.54;0.62;1" />
      </circle>

      {/* ── impact ring when override snaps in ── */}
      <circle cx="78" cy="50" fill="none" stroke="currentColor" strokeWidth="1.2">
        <animate attributeName="r"       values="0;0;5;20;0;0" dur="4s" repeatCount="indefinite" keyTimes="0;0.5;0.52;0.64;0.7;1" />
        <animate attributeName="opacity" values="0;0;0.7;0;0;0" dur="4s" repeatCount="indefinite" keyTimes="0;0.5;0.52;0.64;0.7;1" />
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
