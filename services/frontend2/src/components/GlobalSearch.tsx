"use client";

import { useState, useRef, type ReactElement } from "react";
import { useRouter } from "next/navigation";

export default function GlobalSearch(): ReactElement {
  const [query, setQuery] = useState("");
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSubmit = (e: React.FormEvent): void => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    router.push(`/persons?q=${encodeURIComponent(q)}`);
    setQuery("");
    inputRef.current?.blur();
  };

  return (
    <form onSubmit={handleSubmit} style={{ flex: 1, maxWidth: 480, position: "relative" }}>
      <div style={{ position: "relative" }}>
        <svg
          width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", pointerEvents: "none", flexShrink: 0 }}
        >
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
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
          }}
          onFocus={(e) => {
            e.currentTarget.style.borderColor = "var(--accent)";
            e.currentTarget.style.boxShadow = "0 0 0 3px rgba(67,97,238,0.1)";
          }}
          onBlur={(e) => {
            e.currentTarget.style.borderColor = "var(--border)";
            e.currentTarget.style.boxShadow = "none";
          }}
        />
        {query && (
          <kbd style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", fontSize: 10, color: "var(--text-muted)", background: "var(--bg-hover)", border: "1px solid var(--border)", borderRadius: 4, padding: "1px 5px", pointerEvents: "none" }}>
            ↵
          </kbd>
        )}
      </div>
    </form>
  );
}
