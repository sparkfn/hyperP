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
  const P  = { x: 50, y: 54 };
  const T  = { x: 50, y: 28 };
  const F1 = { x: 73, y: 67 };
  const F2 = { x: 27, y: 67 };

  return (
    <svg width="200" height="200" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>

      {/* ── always-flowing edge dashes ── */}
      <line x1={P.x} y1={P.y} x2={F1.x} y2={F1.y} stroke="currentColor" strokeWidth="0.8" strokeDasharray="2 3">
        <animate attributeName="opacity" values="0.15;0.28;0.15" dur="1.8s" repeatCount="indefinite" />
        <animate attributeName="strokeDashoffset" values="0;-20" dur="1.2s" repeatCount="indefinite" />
      </line>
      <line x1={P.x} y1={P.y} x2={F2.x} y2={F2.y} stroke="currentColor" strokeWidth="0.8" strokeDasharray="2 3">
        <animate attributeName="opacity" values="0.15;0.28;0.15" dur="2.2s" repeatCount="indefinite" begin="0.4s" />
        <animate attributeName="strokeDashoffset" values="0;-20" dur="1.5s" repeatCount="indefinite" />
      </line>
      {/* target edge — dims then flares */}
      <line x1={P.x} y1={P.y} x2={T.x} y2={T.y} stroke="currentColor" strokeDasharray="2 3">
        <animate attributeName="strokeWidth" values="0.8;0.8;0.3;2.8;2.8;0.8" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.2;0.42;0.52;0.72;1" />
        <animate attributeName="opacity"     values="0.18;0.18;0.04;0.9;0.9;0.18" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.2;0.42;0.52;0.72;1" />
        <animate attributeName="strokeDashoffset" values="0;-20" dur="0.9s" repeatCount="indefinite" />
      </line>

      {/* ── field nodes: continuous breathing ── */}
      <circle cx={F1.x} cy={F1.y} fill="currentColor">
        <animate attributeName="r"       values="4.5;5.5;4.5;4;4.5" dur="2.3s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.25;0.42;0.25;0.2;0.25" dur="2.3s" repeatCount="indefinite" />
      </circle>
      <circle cx={F2.x} cy={F2.y} fill="currentColor">
        <animate attributeName="r"       values="4.5;4;4.5;5.5;4.5" dur="2.7s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.25;0.2;0.25;0.42;0.25" dur="2.7s" repeatCount="indefinite" />
      </circle>

      {/* ── person: continuous pulse ── */}
      <circle cx={P.x} cy={P.y} fill="currentColor">
        <animate attributeName="r"       values="9;10.5;9;9.5;9" dur="1.9s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0.55;0.72;0.55;0.65;0.55" dur="1.9s" repeatCount="indefinite" />
      </circle>

      {/* ── target field node: breathes then gets replaced ── */}
      <circle cx={T.x} cy={T.y} fill="currentColor">
        <animate attributeName="r"       values="4.5;5.2;4.5;4.5;1.5;0;4.5" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.18;0.32;0.38;0.48;0.54;1" />
        <animate attributeName="opacity" values="0.5;0.65;0.5;0.5;0.1;0;0.5"  dur="2.6s" repeatCount="indefinite" keyTimes="0;0.18;0.32;0.38;0.48;0.54;1" />
      </circle>

      {/* ── source node: wide sweeping arc from 90,5 → 20,12 → 50,28 ── */}
      <circle fill="currentColor">
        <animate attributeName="cx" values="90;90;20;50;50;90" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.06;0.34;0.52;0.72;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="cy" values="5;5;12;28;28;5"     dur="2.6s" repeatCount="indefinite" keyTimes="0;0.06;0.34;0.52;0.72;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="r"       values="5;5.5;6.5;7;6;5" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.06;0.34;0.54;0.62;1" />
        <animate attributeName="opacity" values="0;0.88;0.95;0.95;0.6;0"    dur="2.6s" repeatCount="indefinite" keyTimes="0;0.08;0.34;0.52;0.72;1" />
      </circle>

      {/* ── trail dots along the arc ── */}
      <circle fill="currentColor">
        <animate attributeName="cx" values="90;90;20;50;50;90" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.06;0.34;0.52;0.72;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" begin="0.08s" />
        <animate attributeName="cy" values="5;5;12;28;28;5"     dur="2.6s" repeatCount="indefinite" keyTimes="0;0.06;0.34;0.52;0.72;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" begin="0.08s" />
        <animate attributeName="r"       values="2.5;3;3.5;2;0;0" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.06;0.34;0.52;0.6;1" begin="0.08s" />
        <animate attributeName="opacity" values="0;0.4;0.5;0.2;0;0" dur="2.6s" repeatCount="indefinite" keyTimes="0;0.08;0.34;0.52;0.6;1" begin="0.08s" />
      </circle>

      {/* ── impact burst ── */}
      <circle cx={T.x} cy={T.y} fill="none" stroke="currentColor" strokeWidth="1.5">
        <animate attributeName="r"       values="0;0;7;22;22;0"   dur="2.6s" repeatCount="indefinite" keyTimes="0;0.5;0.52;0.64;0.68;1" />
        <animate attributeName="opacity" values="0;0;0.8;0;0;0"   dur="2.6s" repeatCount="indefinite" keyTimes="0;0.5;0.52;0.64;0.68;1" />
      </circle>
      <circle cx={T.x} cy={T.y} fill="none" stroke="currentColor" strokeWidth="0.8">
        <animate attributeName="r"       values="0;0;7;28;28;0"   dur="2.6s" repeatCount="indefinite" keyTimes="0;0.5;0.54;0.7;0.74;1" />
        <animate attributeName="opacity" values="0;0;0.4;0;0;0"   dur="2.6s" repeatCount="indefinite" keyTimes="0;0.5;0.54;0.7;0.74;1" />
      </circle>

      {/* ── lock dot ── */}
      <circle cx={T.x} cy={T.y} fill="currentColor">
        <animate attributeName="r"       values="0;0;4.5;4.5;0"     dur="2.6s" repeatCount="indefinite" keyTimes="0;0.54;0.58;0.78;1" />
        <animate attributeName="opacity" values="0;0;0.9;0.9;0"      dur="2.6s" repeatCount="indefinite" keyTimes="0;0.54;0.58;0.78;1" />
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
