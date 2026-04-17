"use client";

import { useCallback, useEffect, useState, type ReactElement } from "react";

import Accordion from "@mui/material/Accordion";
import AccordionDetails from "@mui/material/AccordionDetails";
import AccordionSummary from "@mui/material/AccordionSummary";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";

import { BffError, bffFetch } from "@/lib/api-client";
import type { EntityPerson, EntitySummary } from "@/lib/api-types";

import EntityPersonsTable, {
  type SortField,
  type SortOrder,
} from "@/components/EntityPersonsTable";

export default function EntitiesPage(): ReactElement {
  const [entities, setEntities] = useState<EntitySummary[] | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load(): Promise<void> {
      try {
        const data = await bffFetch<EntitySummary[]>("/api/entities");
        if (!cancelled) setEntities(data);
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof BffError || err instanceof Error
            ? err.message : "Failed to load entities.";
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }
  if (error) return <Alert severity="error">{error}</Alert>;
  if (!entities || entities.length === 0) {
    return <Alert severity="info">No entities found.</Alert>;
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Entities
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Persons grouped by business entity.
        </Typography>
      </Box>
      {entities.map((entity) => (
        <EntityAccordion
          key={entity.entity_key}
          entity={entity}
          expanded={expandedKey === entity.entity_key}
          onToggle={(isExpanded) =>
            setExpandedKey(isExpanded ? entity.entity_key : null)
          }
        />
      ))}
    </Stack>
  );
}

interface EntityAccordionProps {
  entity: EntitySummary;
  expanded: boolean;
  onToggle: (isExpanded: boolean) => void;
}

function EntityAccordion({ entity, expanded, onToggle }: EntityAccordionProps): ReactElement {
  const [persons, setPersons] = useState<EntityPerson[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<number>(0);
  const [rowsPerPage, setRowsPerPage] = useState<number>(20);
  const [sortBy, setSortBy] = useState<SortField>("preferred_full_name");
  const [sortOrder, setSortOrder] = useState<SortOrder>("asc");

  const fetchPage = useCallback(
    async (pg: number, rpp: number, sb: SortField, so: SortOrder): Promise<void> => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          limit: String(rpp),
          sort_by: sb,
          sort_order: so,
        });
        const offset = pg * rpp;
        if (offset > 0) {
          params.set("cursor", btoa(String(offset)));
        }
        const url = `/api/entities/${encodeURIComponent(entity.entity_key)}/persons?${params.toString()}`;
        const data = await bffFetch<EntityPerson[]>(url);
        setPersons(data);
      } catch (err: unknown) {
        const msg = err instanceof BffError || err instanceof Error
          ? err.message : "Failed to load persons.";
        setError(msg);
      } finally {
        setLoading(false);
      }
    },
    [entity.entity_key],
  );

  function handleExpand(_: React.SyntheticEvent, isExpanded: boolean): void {
    onToggle(isExpanded);
    if (isExpanded && persons.length === 0 && !loading) {
      void fetchPage(0, rowsPerPage, sortBy, sortOrder);
    }
  }

  function handlePageChange(newPage: number): void {
    setPage(newPage);
    void fetchPage(newPage, rowsPerPage, sortBy, sortOrder);
  }

  function handleRowsPerPageChange(newRowsPerPage: number): void {
    setRowsPerPage(newRowsPerPage);
    setPage(0);
    void fetchPage(0, newRowsPerPage, sortBy, sortOrder);
  }

  function handleSortChange(field: SortField): void {
    const newOrder: SortOrder =
      sortBy === field && sortOrder === "asc" ? "desc" : "asc";
    setSortBy(field);
    setSortOrder(newOrder);
    setPage(0);
    void fetchPage(0, rowsPerPage, field, newOrder);
  }

  return (
    <Accordion expanded={expanded} onChange={handleExpand}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={2} alignItems="center">
          <Typography variant="h6" fontWeight={600}>
            {entity.display_name ?? entity.entity_key}
          </Typography>
          {entity.entity_type ? (
            <Chip label={entity.entity_type} size="small" variant="outlined" />
          ) : null}
          {entity.country_code ? (
            <Chip label={entity.country_code} size="small" variant="outlined" />
          ) : null}
          <Chip label={`${entity.person_count} persons`} size="small" color="primary" />
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        {!expanded ? null : loading && persons.length === 0 ? (
          <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
            <CircularProgress size={24} />
          </Box>
        ) : error ? (
          <Alert severity="error">{error}</Alert>
        ) : persons.length === 0 ? (
          <Alert severity="info">No persons found.</Alert>
        ) : (
          <EntityPersonsTable
            persons={persons}
            totalCount={entity.person_count}
            page={page}
            rowsPerPage={rowsPerPage}
            sortBy={sortBy}
            sortOrder={sortOrder}
            loading={loading}
            onSortChange={handleSortChange}
            onPageChange={handlePageChange}
            onRowsPerPageChange={handleRowsPerPageChange}
          />
        )}
      </AccordionDetails>
    </Accordion>
  );
}
