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
  // Person node at center. 4 field nodes around it.
  // Field node at top-right gets overridden by an incoming source node.
  // cx/cy of the 4 field nodes (evenly spaced around center 50,50, r=28)
  const cx = 50; const cy = 50; const r = 28;
  const fields = [
    { fx: cx + r * Math.cos(-Math.PI / 4),  fy: cy + r * Math.sin(-Math.PI / 4)  }, // top-right  ← target
    { fx: cx + r * Math.cos( Math.PI / 4),  fy: cy + r * Math.sin( Math.PI / 4)  }, // bot-right
    { fx: cx + r * Math.cos( 3*Math.PI / 4),fy: cy + r * Math.sin( 3*Math.PI / 4)}, // bot-left
    { fx: cx + r * Math.cos(-3*Math.PI / 4),fy: cy + r * Math.sin(-3*Math.PI / 4)}, // top-left
  ] as const;
  const target = fields[0]!; // top-right field node gets overridden
  // source node starts far top-right corner
  const srcX = 92; const srcY = 8;

  return (
    <svg width="200" height="200" viewBox="0 0 100 100" style={{ color: "var(--text-secondary, #6b7280)" }}>
      {/* edges: person → each field */}
      {fields.map((f, i) => (
        <line key={i} x1={cx} y1={cy} x2={f.fx} y2={f.fy} stroke="currentColor" strokeWidth="1" opacity={i === 0 ? undefined : "0.25"}>
          {i === 0 && <animate attributeName="opacity" values="0.25;0.25;0;0.9;0.9;0.25" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.3;0.48;0.56;0.78;1" />}
        </line>
      ))}

      {/* person node — center */}
      <circle cx={cx} cy={cy} r="9" fill="currentColor" opacity="0.6" />

      {/* field nodes — non-target stay static */}
      {fields.slice(1).map((f, i) => (
        <circle key={i} cx={f.fx} cy={f.fy} r="5" fill="currentColor" opacity="0.3" />
      ))}

      {/* target field node — fades out when overridden */}
      <circle cx={target.fx} cy={target.fy} r="5" fill="currentColor">
        <animate attributeName="opacity" values="0.55;0.55;0.08;0.08;0.55" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.42;0.5;0.78;1" />
      </circle>

      {/* source (override) node — travels from corner to target position */}
      <circle cx={srcX} cy={srcY} r="5.5" fill="currentColor">
        <animate attributeName="cx" values={`${srcX};${srcX};${target.fx};${target.fx};${srcX}`} dur="3.2s" repeatCount="indefinite" keyTimes="0;0.28;0.5;0.78;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="cy" values={`${srcY};${srcY};${target.fy};${target.fy};${srcY}`} dur="3.2s" repeatCount="indefinite" keyTimes="0;0.28;0.5;0.78;1" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1" />
        <animate attributeName="opacity" values="0.2;0.85;0.85;0.85;0.2" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.3;0.5;0.78;1" />
        <animate attributeName="r" values="5.5;5.5;6.5;5.5;5.5" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.3;0.52;0.6;1" />
      </circle>

      {/* pulse ring at target when source arrives */}
      <circle cx={target.fx} cy={target.fy} r="6" fill="none" stroke="currentColor" strokeWidth="1.2">
        <animate attributeName="r" values="6;6;14;14;6" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.5;0.6;0.65;1" />
        <animate attributeName="opacity" values="0;0.6;0;0;0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.5;0.62;0.65;1" />
      </circle>

      {/* dashed edge from source to person — appears after override */}
      <line x1={cx} y1={cy} x2={target.fx} y2={target.fy} stroke="currentColor" strokeWidth="1.8" strokeDasharray="0">
        <animate attributeName="opacity" values="0;0;0.75;0.75;0" dur="3.2s" repeatCount="indefinite" keyTimes="0;0.54;0.58;0.78;1" />
      </line>
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
