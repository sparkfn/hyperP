"use client";
import { notFound } from "next/navigation";
import { useEffect, useState, type ReactElement } from "react";

if (process.env.NODE_ENV !== "development") notFound();

function MergeAnimation(): ReactElement {
  return (
    <svg width="200" height="200" viewBox="0 0 100 100" overflow="visible" style={{ color: "var(--text-secondary, #6b7280)" }}>
      {/* ── System A — whole group translates: left edge → home (28,50) → center (50,50) ── */}
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

      {/* ── System B — whole group translates: right edge → home (72,50) → center (50,50) ── */}
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

      {/* ── Merged state — center (50,50), 2 rings ── */}
      {/* Merge pulse ring */}
      <circle cx="50" cy="50" r="0" fill="none" stroke="currentColor" strokeWidth="1.5">
        <animate attributeName="r"       values="0;0;8;38;0;0" keyTimes="0;0.18;0.21;0.34;0.38;1" dur="8s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0;0;0.5;0;0;0"  keyTimes="0;0.18;0.21;0.34;0.38;1" dur="8s" repeatCount="indefinite" />
      </circle>
      {/* Merged center */}
      <circle cx="50" cy="50" r="0" fill="currentColor">
        <animate attributeName="r"       values="0;0;8;8;0;0"     keyTimes="0;0.18;0.24;0.82;0.90;1" dur="8s" repeatCount="indefinite" />
        <animate attributeName="opacity" values="0;0;0.7;0.7;0;0" keyTimes="0;0.18;0.24;0.82;0.90;1" dur="8s" repeatCount="indefinite" />
      </circle>
      {/* Inner ring (r=22) */}
      <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4">
        <animate attributeName="opacity" values="0;0;0;0.25;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
      </circle>
      {/* Outer ring (r=36) */}
      <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3">
        <animate attributeName="opacity" values="0;0;0;0.18;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
      </circle>
      {/* Inner ring nodes — same angles/sizes as GraphAnimation */}
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
      {/* Outer ring nodes — 60°/180°/300° matching GraphAnimation */}
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
  );
}

function OverrideAnimation(): ReactElement {
  // Override dots live INSIDE the same rotating group as their target node.
  // The group rotates together, so the dot naturally "chases" the orbiting node.
  // Dot cx animates: far outside (106) → node cx (72/86) → invisible reset.
  // Different dur from the group's orbit creates a spiral approach in world space.
  return (
    <svg width="200" height="200" viewBox="0 0 100 100" overflow="visible" style={{ color: "var(--text-secondary, #6b7280)" }}>
      <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4" opacity="0.25" />
      <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3" opacity="0.18" />
      <circle cx="50" cy="50" r="7" fill="currentColor" opacity="0.55" />

      {/* inner A — with override dot, begin=0s dur=6s */}
      <g>
        <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
        <circle cx="72" cy="50" r="5" fill="currentColor" opacity="0.65" />
        <circle cy="50" fill="currentColor">
          <animate attributeName="cx"      values="106;106;72;72;106;106" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="6s"  begin="0s"   repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
          <animate attributeName="r"       values="3.5;3.5;5;3.5;3.5;3.5" keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="6s"  begin="0s"   repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0.82;0.88;0;0;0"      keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="6s"  begin="0s"   repeatCount="indefinite" />
        </circle>
        <circle cx="72" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
          <animate attributeName="r"       values="0;0;5;16;16;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="6s"  begin="0s"  repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.7;0;0;0"  keyTimes="0;0.48;0.5;0.62;0.66;1" dur="6s"  begin="0s"  repeatCount="indefinite" />
        </circle>
      </g>

      {/* inner B — with override dot, begin=2.4s dur=7.5s */}
      <g>
        <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
        <circle cx="72" cy="50" r="4" fill="currentColor" opacity="0.5" />
        <circle cy="50" fill="currentColor">
          <animate attributeName="cx"      values="106;106;72;72;106;106" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="7.5s" begin="2.4s"  repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
          <animate attributeName="r"       values="3;3;4.5;3;3;3"         keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="7.5s" begin="2.4s"  repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0.75;0.82;0;0;0"     keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="7.5s" begin="2.4s"  repeatCount="indefinite" />
        </circle>
        <circle cx="72" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
          <animate attributeName="r"       values="0;0;4;14;14;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="7.5s" begin="2.4s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.65;0;0;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="7.5s" begin="2.4s" repeatCount="indefinite" />
        </circle>
      </g>

      {/* outer 1 — with override dot, begin=1s dur=8s */}
      <g>
        <animateTransform attributeName="transform" type="rotate" from="60 50 50" to="-300 50 50" dur="5s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
        <circle cx="86" cy="50" r="4" fill="currentColor" opacity="0.55" />
        <circle cy="50" fill="currentColor">
          <animate attributeName="cx"      values="108;108;86;86;108;108" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="8s"  begin="1s"   repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
          <animate attributeName="r"       values="3.5;3.5;5;3.5;3.5;3.5" keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="8s"  begin="1s"   repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0.78;0.85;0;0;0"     keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="8s"  begin="1s"   repeatCount="indefinite" />
        </circle>
        <circle cx="86" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
          <animate attributeName="r"       values="0;0;5;16;16;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="8s"  begin="1s"  repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.65;0;0;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="8s"  begin="1s"  repeatCount="indefinite" />
        </circle>
      </g>

      {/* outer 2 */}
      <g>
        <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="-180 50 50" dur="5s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
        <circle cx="86" cy="50" r="3.5" fill="currentColor" opacity="0.45" />
      </g>

      {/* outer 3 — with override dot, begin=3.8s dur=9s */}
      <g>
        <animateTransform attributeName="transform" type="rotate" from="300 50 50" to="-60 50 50" dur="5s" repeatCount="indefinite" />
        <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
        <circle cx="86" cy="50" r="3" fill="currentColor" opacity="0.4" />
        <circle cy="50" fill="currentColor">
          <animate attributeName="cx"      values="108;108;86;86;108;108" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="9s"  begin="3.8s"  repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
          <animate attributeName="r"       values="3;3;4.5;3;3;3"         keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="9s"  begin="3.8s"  repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0.7;0.8;0;0;0"       keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="9s"  begin="3.8s"  repeatCount="indefinite" />
        </circle>
        <circle cx="86" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
          <animate attributeName="r"       values="0;0;4;14;14;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="9s"  begin="3.8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.6;0;0;0"  keyTimes="0;0.48;0.5;0.62;0.66;1" dur="9s"  begin="3.8s" repeatCount="indefinite" />
        </circle>
      </g>
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

function EmptyStateAnimation(): ReactElement {
  return (
    <svg width="120" height="120" viewBox="0 0 80 80" fill="none" style={{ color: "var(--text-secondary, #6b7280)", animation: "emptyFloat 3.4s ease-in-out infinite" }}>
      <style>{`@keyframes emptyFloat { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-6px)} }`}</style>
      <circle cx="40" cy="40" r="17" stroke="currentColor" strokeWidth="0.7" strokeDasharray="2.5 2.5" opacity="0.20" />
      <circle cx="40" cy="40" r="28" stroke="currentColor" strokeWidth="0.5" strokeDasharray="2 3"   opacity="0.13" />
      <line x1="40" y1="40" x2="22" y2="24" stroke="currentColor" strokeWidth="0.9" strokeDasharray="2 2.5" strokeLinecap="round" opacity="0.22" />
      <line x1="40" y1="40" x2="60" y2="30" stroke="currentColor" strokeWidth="0.9" strokeDasharray="2 2.5" strokeLinecap="round" opacity="0.17" />
      <line x1="40" y1="40" x2="56" y2="58" stroke="currentColor" strokeWidth="0.9" strokeDasharray="2 2.5" strokeLinecap="round" opacity="0.17" />
      <circle cx="40" cy="40" r="7" fill="currentColor" opacity="0.50" />
      <circle cx="10" cy="14" r="2.5" fill="currentColor" opacity="0.22" />
      <circle cx="70" cy="16" r="2"   fill="currentColor" opacity="0.17" />
      <circle cx="68" cy="68" r="2"   fill="currentColor" opacity="0.15" />
      <circle cx="13" cy="66" r="1.5" fill="currentColor" opacity="0.12" />
    </svg>
  );
}

export default function AnimationsPage(): ReactElement {
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    const stored = localStorage.getItem("theme");
    const initial = stored === "light" ? "light" : "dark";
    setTheme(initial);
    document.documentElement.setAttribute("data-theme", initial);
  }, []);

  function toggleTheme(): void {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  }

  return (
    <div style={{ minHeight: "100vh", background: "var(--bg-app)", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 48, padding: 40, fontFamily: "system-ui, sans-serif", position: "relative" }}>
      <button
        onClick={toggleTheme}
        style={{ position: "absolute", top: 20, right: 20, background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8, padding: "6px 14px", cursor: "pointer", color: "var(--text-secondary)", fontSize: 12, letterSpacing: "0.05em" }}
      >
        {theme === "dark" ? "Light" : "Dark"}
      </button>
      <h1 style={{ color: "var(--text-secondary)", fontSize: 13, letterSpacing: "0.1em", textTransform: "uppercase", margin: 0 }}>Animation Preview</h1>

      <div style={{ display: "flex", gap: 64, flexWrap: "wrap", justifyContent: "center" }}>
        {([
          { label: "Graph loading", el: <GraphAnimation /> },
          { label: "Merge loading", el: <MergeAnimation /> },
          { label: "Override loading", el: <OverrideAnimation /> },
          { label: "Empty state", el: <EmptyStateAnimation /> },
        ] as const).map(({ label, el }) => (
          <div key={label} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 16 }}>
            <div style={{ width: 240, height: 240, background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 16, display: "flex", alignItems: "center", justifyContent: "center" }}>
              {el}
            </div>
            <p style={{ color: "var(--text-muted)", fontSize: 12, margin: 0 }}>{label}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
