"use client";

import { useEffect, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";

import { API_HEALTH_PATH } from "@/lib/route-paths";
import type { HealthResponse } from "@/lib/api-types-ops";

type Status = "ok" | "down" | "loading";

function colorFor(status: Status): string {
  if (status === "ok") return "#2e7d32";
  if (status === "down") return "#c62828";
  return "#9e9e9e";
}

function labelFor(status: Status): string {
  if (status === "ok") return "API healthy";
  if (status === "down") return "API unreachable";
  return "Checking API...";
}

function isHealthResponse(value: unknown): value is HealthResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    typeof value.status === "string"
  );
}

export default function HealthIndicator(): ReactElement {
  const [status, setStatus] = useState<Status>("loading");

  useEffect(() => {
    let cancelled: boolean = false;

    async function check(): Promise<void> {
      try {
        const response: Response = await fetch(API_HEALTH_PATH);
        const parsed: unknown = await response.json();
        if (!response.ok || !isHealthResponse(parsed)) throw new Error("Health check failed.");
        const res: HealthResponse = parsed;
        if (cancelled) return;
        setStatus(res.status === "ok" ? "ok" : "down");
      } catch {
        if (cancelled) return;
        setStatus("down");
      }
    }

    void check();
    const interval: ReturnType<typeof setInterval> = setInterval(() => {
      void check();
    }, 30_000);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  return (
    <Tooltip title={labelFor(status)}>
      <Box
        aria-label={labelFor(status)}
        sx={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          backgroundColor: colorFor(status),
          display: "inline-block",
        }}
      />
    </Tooltip>
  );
}
