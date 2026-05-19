"use client";

import { useEffect, useId, useState } from "react";
import { bffFetch } from "./api-client";
import { useSetLoading } from "./LoadingContext";

interface BffListResult<T> {
  data: T[];
  loading: boolean;
  error: string | null;
}

// Module-level cache — persists across navigations in the same session.
// Stale-while-revalidate: return cached data immediately, fetch fresh in background.
const listCache = new Map<string, unknown[]>();

export function useBffList<T>(url: string, errorMessage?: string): BffListResult<T> {
  const cached = listCache.get(url) as T[] | undefined;
  const [data, setData] = useState<T[]>(cached ?? []);
  const [loading, setLoading] = useState(cached === undefined);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const setGlobalLoading = useSetLoading();

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    if (!listCache.has(url)) { setLoading(true); setGlobalLoading(id, true); }
    void bffFetch<T[]>(url, { signal: controller.signal })
      .then((res) => {
        listCache.set(url, res);
        if (!cancelled) { setData(res); setError(null); }
      })
      .catch(() => { if (!cancelled && !listCache.has(url)) setError(errorMessage ?? "Failed to load data."); })
      .finally(() => { if (!cancelled) { setLoading(false); setGlobalLoading(id, false); } });
    return () => { cancelled = true; controller.abort(); setGlobalLoading(id, false); };
  }, [url, errorMessage, id, setGlobalLoading]);

  return { data, loading, error };
}
