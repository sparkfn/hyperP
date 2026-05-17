"use client";

import { useState, useRef, useEffect, useCallback, type ReactElement, type KeyboardEvent } from "react";
import { useRouter } from "next/navigation";

import { bffFetchEnvelope } from "@/lib/api-client";
import type { Person } from "@/lib/api-types";

const AVATAR_COLORS = ["#4361ee", "#7c3aed", "#0891b2", "#059669", "#d97706", "#dc2626"];
function avatarBg(name: string): string {
  return AVATAR_COLORS[name.charCodeAt(0) % AVATAR_COLORS.length] ?? "#4361ee";
}
function ringColor(s: number): string {
  if (s >= 0.8) return "#22c55e";
  if (s >= 0.5) return "#f59e0b";
  return "#ef4444";
}
function statusCol(s: string): string {
  if (s === "active") return "#22c55e";
  if (s === "merged") return "#3b82f6";
  return "#94a3b8";
}

interface SearchResult {
  personId: string;
  name: string;
  email: string | null;
  phone: string | null;
  status: string;
  completeness: number;
}

function AvatarRing({ name, completeness }: { name: string; completeness: number }): ReactElement {
  const initials = name.split(" ").filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  const circ = 2 * Math.PI * 14;
  const dash = circ * completeness;
  const gap = circ - dash;
  const col = ringColor(completeness);
  return (
    <div style={{ position: "relative", width: 34, height: 34, flexShrink: 0 }}>
      <svg width="34" height="34" viewBox="0 0 34 34" fill="none" style={{ position: "absolute", inset: 0 }}>
        <circle cx="17" cy="17" r="14" stroke={col} strokeWidth="1.5" opacity={0.2} />
        <circle cx="17" cy="17" r="14" stroke={col} strokeWidth="1.5" strokeLinecap="round"
          strokeDasharray={`${dash} ${gap}`} transform="rotate(-90 17 17)" />
      </svg>
      <div style={{ position: "absolute", inset: 3, borderRadius: "50%", background: avatarBg(name || "?"), display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: 11, fontWeight: 700 }}>
        {initials || "?"}
      </div>
    </div>
  );
}

const SKELETON_ROW = (
  <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 12px" }}>
    <div style={{ width: 34, height: 34, borderRadius: "50%", background: "var(--border)", flexShrink: 0, animation: "pulse 1.5s ease-in-out infinite" }} />
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 5 }}>
      <div style={{ height: 12, width: "50%", borderRadius: 4, background: "var(--border)", animation: "pulse 1.5s ease-in-out infinite" }} />
      <div style={{ height: 10, width: "35%", borderRadius: 4, background: "var(--border)", animation: "pulse 1.5s ease-in-out infinite" }} />
    </div>
  </div>
);

export default function GlobalSearch(): ReactElement {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Debounced search
  useEffect(() => {
    const q = query.trim();
    if (q.length < 3) { setResults([]); setLoading(false); return; }
    setLoading(true);
    let cancelled = false;
    void bffFetchEnvelope<Person[]>(`/bff/persons/search?q=${encodeURIComponent(q)}&limit=8`)
      .then((res) => {
        if (!cancelled) setResults((res.data ?? []).map((p) => ({
          personId: p.person_id,
          name: p.preferred_full_name ?? p.person_id,
          email: p.preferred_email,
          phone: p.preferred_phone,
          status: p.status,
          completeness: p.profile_completeness_score,
        })));
      })
      .catch(() => { if (!cancelled) setResults([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [query]);

  // Reset highlight when results change
  useEffect(() => { setHighlighted(-1); }, [results]);

  const navigate = useCallback((personId: string) => {
    router.push(`/persons/${encodeURIComponent(personId)}`);
    setQuery("");
    setOpen(false);
    inputRef.current?.blur();
  }, [router]);

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (!open) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlighted((h) => Math.min(h + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlighted((h) => Math.max(h - 1, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (highlighted >= 0 && results[highlighted]) {
        navigate(results[highlighted].personId);
      } else if (results[0]) {
        navigate(results[0].personId);
      } else {
        router.push(`/persons?q=${encodeURIComponent(query.trim())}`);
        setQuery(""); setOpen(false);
      }
    } else if (e.key === "Escape") {
      setOpen(false);
      inputRef.current?.blur();
    }
  };

  const showDropdown = open && query.trim().length >= 3;

  return (
    <div ref={containerRef} style={{ flex: 1, maxWidth: 480, position: "relative" }}>
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }`}</style>
      {/* Input */}
      <div style={{ position: "relative" }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onKeyDown={handleKeyDown}
          placeholder="Search persons by name, phone, email..."
          style={{
            width: "100%",
            padding: "8px 12px 8px 36px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "var(--bg-app)",
            color: "var(--text-primary)",
            fontSize: 13,
            outline: "none",
            transition: "border-color 0.15s, box-shadow 0.15s",
            boxSizing: "border-box",
          }}
          onFocusCapture={(e) => {
            e.currentTarget.style.borderColor = "var(--accent, #4361ee)";
            e.currentTarget.style.boxShadow = "0 0 0 3px rgba(67,97,238,0.1)";
          }}
          onBlurCapture={(e) => {
            e.currentTarget.style.borderColor = "var(--border)";
            e.currentTarget.style.boxShadow = "none";
          }}
        />
      </div>

      {/* Dropdown */}
      {showDropdown && (
        <div style={{
          position: "absolute",
          top: "calc(100% + 6px)",
          left: 0,
          right: 0,
          background: "var(--bg-card)",
          border: "1px solid var(--border)",
          borderRadius: 12,
          boxShadow: "0 8px 32px rgba(0,0,0,0.18)",
          zIndex: 1400,
          overflow: "hidden",
        }}>
          {loading ? (
            <div style={{ padding: "4px 0" }}>
              {SKELETON_ROW}
              {SKELETON_ROW}
            </div>
          ) : results.length === 0 ? (
            <div style={{ padding: "14px 16px", fontSize: 13, color: "var(--text-muted)", textAlign: "center" }}>
              No results
            </div>
          ) : (
            <div style={{ padding: "4px 0" }}>
              {results.map((r, i) => (
                <div
                  key={r.personId}
                  onMouseDown={() => navigate(r.personId)}
                  onMouseEnter={() => setHighlighted(i)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "8px 12px",
                    cursor: "pointer",
                    background: highlighted === i ? "var(--bg-surface-2, rgba(0,0,0,0.04))" : "transparent",
                    transition: "background 0.1s",
                  }}
                >
                  <AvatarRing name={r.name} completeness={r.completeness} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", lineHeight: 1.3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {r.name}
                    </div>
                    <div style={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {r.email ?? r.phone ?? r.personId}
                    </div>
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2, flexShrink: 0 }}>
                    <span style={{ fontSize: 11, color: statusCol(r.status), fontWeight: 600, textTransform: "capitalize" }}>{r.status}</span>
                    <span style={{ fontSize: 11, color: ringColor(r.completeness), fontWeight: 600 }}>{Math.round(r.completeness * 100)}%</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
