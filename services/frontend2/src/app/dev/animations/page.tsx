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
  // Layout: person center (50,54), 3 field nodes around r=26
  // top(50,28), bottom-right(72,67), bottom-left(28,67)
  // Source node arrives from top-right (88,8) → replaces top field node
  const P = { x: 50, y: 54 };
  const fields = [
    { x: 50, y: 28 },   // top ← target
    { x: 73, y: 67 },   // bottom-right
    { x: 27, y: 67 },   // bottom-left
  ] as const;
  const T = fields[0]!;
  const S = { x: 88, y: 8 };

  return (
    <svg width="200" height="200" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>

      {/* ── static edges + nodes (non-target) ── */}
      {fields.slice(1).map((f, i) => (
        <g key={i}>
          <line x1={P.x} y1={P.y} x2={f.x} y2={f.y} stroke="currentColor" strokeWidth="0.8" opacity="0.2" />
          <circle cx={f.x} cy={f.y} r="4.5" fill="currentColor" opacity="0.25" />
        </g>
      ))}

      {/* ── person node ── */}
      <circle cx={P.x} cy={P.y} r="9" fill="currentColor" opacity="0.6" />

      {/* ── target edge: person → top field ── */}
      <line x1={P.x} y1={P.y} x2={T.x} y2={T.y} stroke="currentColor">
        <animate attributeName="strokeWidth" values="0.8;0.8;0.8;2.2;2.2;0.8" dur="3.5s" repeatCount="indefinite" keyTimes="0;0.28;0.48;0.58;0.78;1" />
        <animate attributeName="opacity"     values="0.2;0.2;0.06;0.75;0.75;0.2" dur="3.5s" repeatCount="indefinite" keyTimes="0;0.28;0.48;0.58;0.78;1" />
      </line>

      {/* ── target field node (old) — shrinks & fades ── */}
      <circle cx={T.x} cy={T.y} r="4.5" fill="currentColor">
        <animate attributeName="r"       values="4.5;4.5;2;2;4.5"     dur="3.5s" repeatCount="indefinite" keyTimes="0;0.3;0.5;0.76;1" />
        <animate attributeName="opacity" values="0.5;0.5;0.05;0.05;0.5" dur="3.5s" repeatCount="indefinite" keyTimes="0;0.3;0.5;0.76;1" />
      </circle>

      {/* ── source node — travels from corner to target slot ── */}
      <circle r="5" fill="currentColor">
        <animate attributeName="cx" values={`${S.x};${S.x};${T.x};${T.x};${S.x}`} dur="3.5s" repeatCount="indefinite" keyTimes="0;0.22;0.52;0.76;1" calcMode="spline" keySplines="0 0 0 0;0.35 0 0.1 1;0 0 0 0;0.35 0 0.1 1" />
        <animate attributeName="cy" values={`${S.y};${S.y};${T.y};${T.y};${S.y}`} dur="3.5s" repeatCount="indefinite" keyTimes="0;0.22;0.52;0.76;1" calcMode="spline" keySplines="0 0 0 0;0.35 0 0.1 1;0 0 0 0;0.35 0 0.1 1" />
        <animate attributeName="r"  values="5;5;6;5.5;5"               dur="3.5s" repeatCount="indefinite" keyTimes="0;0.22;0.54;0.62;1" />
        <animate attributeName="opacity" values="0;0.8;0.9;0.9;0"      dur="3.5s" repeatCount="indefinite" keyTimes="0;0.24;0.52;0.76;1" />
      </circle>

      {/* ── impact pulse ring at target ── */}
      <circle cx={T.x} cy={T.y} fill="none" stroke="currentColor" strokeWidth="1">
        <animate attributeName="r"       values="0;0;6;18;18;0"  dur="3.5s" repeatCount="indefinite" keyTimes="0;0.5;0.52;0.64;0.68;1" />
        <animate attributeName="opacity" values="0;0;0.65;0;0;0" dur="3.5s" repeatCount="indefinite" keyTimes="0;0.5;0.52;0.64;0.68;1" />
      </circle>

      {/* ── lock dot: stays at target after override ── */}
      <circle cx={T.x} cy={T.y} fill="currentColor">
        <animate attributeName="r"       values="0;0;0;3.5;3.5;0"    dur="3.5s" repeatCount="indefinite" keyTimes="0;0.52;0.56;0.6;0.78;1" />
        <animate attributeName="opacity" values="0;0;0;0.85;0.85;0"  dur="3.5s" repeatCount="indefinite" keyTimes="0;0.52;0.56;0.6;0.78;1" />
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
