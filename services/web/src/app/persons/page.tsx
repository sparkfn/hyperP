"use client";

import { Suspense, useCallback, useMemo, useState, type ReactElement } from "react";
import { useSearchParams } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import PersonsFilterPanel, {
  DEFAULT_FILTERS,
  type PersonsFilters,
} from "@/components/PersonsFilterPanel";
import PersonsListPager from "@/components/PersonsListPager";
import PersonsListTable, {
  type SortField,
  type SortOrder,
} from "@/components/PersonsListTable";

import { usePersonSelection, usePersonsFetch, useEntitiesList } from "./hooks";
import { countActiveFilters } from "./query";

const ROWS_PER_PAGE_OPTIONS: readonly number[] = [10, 25, 50, 100] as const;

export default function PersonsListPage(): ReactElement {
  return (
    <Suspense fallback={null}>
      <PersonsListPageInner />
    </Suspense>
  );
}

function PersonsListPageInner(): ReactElement {
  const searchParams = useSearchParams();
  const initialEntityKey: string = searchParams.get("entity_key") ?? "";

  const [filters, setFilters] = useState<PersonsFilters>({
    ...DEFAULT_FILTERS,
    entity_key: initialEntityKey,
  });
  const [sortBy, setSortBy] = useState<SortField>("updated_at");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [pageIndex, setPageIndex] = useState<number>(0);
  const [rowsPerPage, setRowsPerPage] = useState<number>(25);

  const entities = useEntitiesList();
  const fetch = usePersonsFetch(filters, sortBy, sortOrder, pageIndex, rowsPerPage);
  const selection = usePersonSelection();

  const totalPages: number = Math.max(1, Math.ceil(fetch.totalCount / rowsPerPage));

  const handleApplyFilters = useCallback((next: PersonsFilters): void => {
    setFilters(next);
    setPageIndex(0);
  }, []);

  const handleClearFilters = useCallback((): void => {
    setFilters(DEFAULT_FILTERS);
    setPageIndex(0);
  }, []);

  const handleSortChange = useCallback((field: SortField): void => {
    setSortBy((prev) => {
      if (prev === field) {
        setSortOrder((o) => (o === "asc" ? "desc" : "asc"));
        return prev;
      }
      setSortOrder("asc");
      return field;
    });
    setPageIndex(0);
  }, []);

  const activeFilterCount: number = useMemo(() => countActiveFilters(filters), [filters]);
  const pageIds: string[] = useMemo(
    () => fetch.persons.map((p) => p.person_id),
    [fetch.persons],
  );

  const firstRow: number = fetch.persons.length > 0 ? pageIndex * rowsPerPage + 1 : 0;
  const lastRow: number = pageIndex * rowsPerPage + fetch.persons.length;

  return (
    <Stack spacing={1.5}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <Typography variant="h5">Persons</Typography>
        {activeFilterCount > 0 ? (
          <Chip
            label={`${activeFilterCount} filter${activeFilterCount === 1 ? "" : "s"}`}
            color="primary"
          />
        ) : null}
        <Box sx={{ flexGrow: 1 }} />
        <Typography variant="caption" color="text.secondary">
          {selection.selected.size > 0 ? `${selection.selected.size} selected` : ""}
        </Typography>
      </Stack>
      <PersonsFilterPanel
        value={filters}
        entities={entities}
        onApply={handleApplyFilters}
        onClear={handleClearFilters}
      />
      {fetch.error ? <Alert severity="error">{fetch.error}</Alert> : null}
      <PersonsListTable
        persons={fetch.persons}
        loading={fetch.loading}
        sortBy={sortBy}
        sortOrder={sortOrder}
        onSortChange={handleSortChange}
        selected={selection.selected}
        onToggleSelect={selection.toggle}
        onToggleSelectAll={() => selection.toggleAll(pageIds)}
      />
      <PersonsListPager
        firstRow={firstRow}
        lastRow={lastRow}
        totalCount={fetch.totalCount}
        pageIndex={pageIndex}
        totalPages={totalPages}
        rowsPerPage={rowsPerPage}
        rowsPerPageOptions={ROWS_PER_PAGE_OPTIONS}
        loading={fetch.loading}
        onGoTo={(p) => setPageIndex(Math.min(Math.max(0, p), totalPages - 1))}
        onRowsPerPageChange={(n) => {
          setRowsPerPage(n);
          setPageIndex(0);
        }}
      />
    </Stack>
  );
}
