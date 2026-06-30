"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { bffFetch } from "./api-client";
import { useSetLoading } from "./LoadingContext";

interface BffListResult<T> {
  data: T[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

// Module-level cache — persists across navigations in the same session.
// Stale-while-revalidate: return cached data immediately, fetch fresh in background.
const listCache = new Map<string, unknown[]>();

export function useBffList<T>(url: string, errorMessage?: string): BffListResult<T> {
  const cached = listCache.get(url) as T[] | undefined;
  const [data, setData] = useState<T[]>(cached ?? []);
  const [loading, setLoading] = useState(cached === undefined);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const id = useId();
  const setGlobalLoading = useSetLoading();

  const refresh = useCallback(() => {
    listCache.delete(url);
    setVersion((v) => v + 1);
  }, [url]);

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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, errorMessage, id, setGlobalLoading, version]);

  return { data, loading, error, refresh };
}
