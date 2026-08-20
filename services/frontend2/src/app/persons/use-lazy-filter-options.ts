"use client";

import { useCallback, useEffect, useState } from "react";

import { bffFetch } from "@/lib/api-client";

export type OptionLoadStatus = "idle" | "loading" | "loaded" | "error";

interface LazyFilterOptions<T> {
  items: T[];
  status: OptionLoadStatus;
  start: () => void;
  retry: () => void;
}

interface LazyFilterOptionsConfig {
  enabled: boolean;
  initialEnabled: boolean;
  path: string;
}

export function useLazyFilterOptions<T>({
  enabled,
  initialEnabled,
  path,
}: LazyFilterOptionsConfig): LazyFilterOptions<T> {
  const [items, setItems] = useState<T[]>([]);
  const [status, setStatus] = useState<OptionLoadStatus>(
    initialEnabled ? "loading" : "idle",
  );

  useEffect(() => {
    if (!enabled || (status !== "idle" && status !== "loading")) return;
    const controller = new AbortController();
    void bffFetch<T[]>(path, { cache: "no-store", signal: controller.signal })
      .then((nextItems) => {
        if (controller.signal.aborted) return;
        setItems(nextItems);
        setStatus("loaded");
      })
      .catch(() => {
        if (!controller.signal.aborted) setStatus("error");
      });
    return () => controller.abort();
  }, [enabled, path, status]);

  const start = useCallback((): void => {
    if (!enabled && status === "idle") setStatus("loading");
  }, [enabled, status]);

  const retry = useCallback((): void => {
    setItems([]);
    setStatus("loading");
  }, []);

  const effectiveStatus = enabled && status === "idle" ? "loading" : status;
  return { items, status: effectiveStatus, start, retry };
}
