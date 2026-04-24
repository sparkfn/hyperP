"use client";

import { useCallback, useEffect, useState } from "react";

import { BffError, bffFetch, bffFetchEnvelope } from "@/lib/api-client";
import type { EntitySummary, ListedPerson } from "@/lib/api-types";
import type { PersonsFilters } from "@/components/PersonsFilterPanel";
import type { SortField, SortOrder } from "@/components/PersonsListTable";

import { buildQuery } from "./query";

export interface PersonsFetchState {
  persons: ListedPerson[];
  totalCount: number;
  loading: boolean;
  error: string | null;
}

export function usePersonsFetch(
  filters: PersonsFilters,
  sortBy: SortField,
  sortOrder: SortOrder,
  pageIndex: number,
  rowsPerPage: number,
): PersonsFetchState {
  const [state, setState] = useState<PersonsFetchState>({
    persons: [],
    totalCount: 0,
    loading: false,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    (async (): Promise<void> => {
      try {
        const qs = buildQuery(filters, sortBy, sortOrder, pageIndex, rowsPerPage);
        const env = await bffFetchEnvelope<ListedPerson[]>(`/bff/persons?${qs}`);
        if (cancelled) return;
        setState({
          persons: env.data,
          totalCount: env.meta.total_count ?? 0,
          loading: false,
          error: null,
        });
      } catch (err: unknown) {
        if (cancelled) return;
        const message: string =
          err instanceof BffError || err instanceof Error ? err.message : "Failed to load persons.";
        setState({ persons: [], totalCount: 0, loading: false, error: message });
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, [filters, sortBy, sortOrder, pageIndex, rowsPerPage]);

  return state;
}

export function useEntitiesList(): EntitySummary[] {
  const [entities, setEntities] = useState<EntitySummary[]>([]);
  useEffect(() => {
    (async (): Promise<void> => {
      try {
        setEntities(await bffFetch<EntitySummary[]>("/bff/entities"));
      } catch {
        setEntities([]);
      }
    })();
  }, []);
  return entities;
}

export interface PersonSelection {
  selected: Set<string>;
  toggle: (personId: string) => void;
  toggleAll: (pageIds: readonly string[]) => void;
}

export function usePersonSelection(): PersonSelection {
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const toggle = useCallback((personId: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(personId)) next.delete(personId);
      else next.add(personId);
      return next;
    });
  }, []);

  const toggleAll = useCallback((pageIds: readonly string[]): void => {
    setSelected((prev) => {
      const allSelected = pageIds.length > 0 && pageIds.every((id) => prev.has(id));
      const next = new Set(prev);
      if (allSelected) {
        pageIds.forEach((id) => next.delete(id));
      } else {
        pageIds.forEach((id) => next.add(id));
      }
      return next;
    });
  }, []);

  return { selected, toggle, toggleAll };
}
