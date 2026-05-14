import type { ReactElement } from "react";

export default function HomePage(): ReactElement {
  return (
    <main
      style={{
        alignItems: "center",
        display: "flex",
        flexDirection: "column",
        fontFamily: "Arial, Helvetica, sans-serif",
        gap: "1rem",
        justifyContent: "center",
        minHeight: "100vh",
        padding: "2rem",
        textAlign: "center",
      }}
    >
      <p style={{ color: "#4b5563", fontSize: "0.875rem", margin: 0 }}>HyperP</p>
      <h1 style={{ fontSize: "3rem", margin: 0 }}>Frontend 2</h1>
      <p style={{ color: "#374151", margin: 0 }}>
        A fresh Next.js service running separately from the original frontend.
      </p>
    </main>
  );
}
