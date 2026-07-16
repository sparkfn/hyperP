"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { bffFetch } from "./api-client";

interface BffListResult<T> {
  data: T[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useBffList<T>(url: string, errorMessage?: string): BffListResult<T> {
  const queryClient = useQueryClient();
  const queryKey = ["bff-list", url] as const;
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => bffFetch<T[]>(url, { signal }),
  });

  const refresh = useCallback((): void => {
    void queryClient.invalidateQueries({ queryKey });
  }, [queryClient, queryKey]);

  return {
    data: query.data ?? [],
    loading: query.isPending,
    error: query.error !== null ? (errorMessage ?? "Failed to load data.") : null,
    refresh,
  };
}
