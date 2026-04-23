"use client";

import { useCallback, useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Checkbox from "@mui/material/Checkbox";
import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Typography from "@mui/material/Typography";

import type { ListedPerson, PersonConnection, SourceRecord } from "@/lib/api-types";
import type { PersonIdentifier } from "@/lib/api-types-person";
import { bffFetch } from "@/lib/api-client";
import PersonGraphDialog from "@/components/PersonGraphDialog";
import PersonRow from "@/components/PersonRow";

export type SortField =
  | "preferred_full_name"
  | "status"
  | "preferred_phone"
  | "preferred_email"
  | "preferred_dob"
  | "preferred_nric"
  | "source_record_count"
  | "connection_count"
  | "entity_count"
  | "identifier_count"
  | "updated_at"
  | "profile_completeness_score";

export type SortOrder = "asc" | "desc";

interface SortableCol {
  field: SortField;
  label: string;
  align?: "left" | "right" | "center";
  sortable: true;
  mono?: boolean;
}

interface NonSortableCol {
  field: "__address";
  label: string;
  align?: "left" | "right" | "center";
  sortable: false;
  mono?: boolean;
}

type ColumnDef = SortableCol | NonSortableCol;

const COLUMNS: readonly ColumnDef[] = [
  { field: "preferred_full_name", label: "Name", sortable: true },
  { field: "status", label: "Status", sortable: true },
  { field: "preferred_phone", label: "Phone", sortable: true },
  { field: "profile_completeness_score", label: "Profile", align: "right", sortable: true },
  { field: "preferred_email", label: "Email", sortable: true },
  { field: "preferred_dob", label: "DOB", sortable: true },
  { field: "preferred_nric", label: "NRIC", sortable: true, mono: true },
  { field: "__address", label: "Address", sortable: false },
  { field: "connection_count", label: "Connections", align: "center", sortable: true },
  { field: "source_record_count", label: "Sources", align: "center", sortable: true },
  { field: "identifier_count", label: "Identifiers", align: "center", sortable: true },
  { field: "entity_count", label: "Entities", align: "center", sortable: true },
  { field: "updated_at", label: "Updated", sortable: true },
] as const;

interface PersonsListTableProps {
  persons: ListedPerson[];
  loading: boolean;
  sortBy: SortField;
  sortOrder: SortOrder;
  onSortChange: (field: SortField) => void;
  selected: Set<string>;
  onToggleSelect: (personId: string) => void;
  onToggleSelectAll: () => void;
}

interface RowContextMenu {
  mouseX: number;
  mouseY: number;
  personId: string;
}

interface GraphDialogState {
  personId: string;
  title: string;
}

interface LazyCache<T> {
  data: Record<string, T[]>;
  loading: Set<string>;
}

function useLazyPersonFetch<T>(
  path: (personId: string) => string,
): { cache: LazyCache<T>; request: (personId: string) => void } {
  const [data, setData] = useState<Record<string, T[]>>({});
  const [loading, setLoading] = useState<Set<string>>(new Set());

  const request = useCallback(
    (personId: string): void => {
      if (data[personId] !== undefined) return;
      if (loading.has(personId)) return;
      setLoading((prev) => new Set(prev).add(personId));
      (async (): Promise<void> => {
        try {
          const result = await bffFetch<T[]>(path(personId));
          setData((prev) => ({ ...prev, [personId]: result }));
        } catch {
          setData((prev) => ({ ...prev, [personId]: [] }));
        } finally {
          setLoading((prev) => {
            const next = new Set(prev);
            next.delete(personId);
            return next;
          });
        }
      })();
    },
    [data, loading, path],
  );

  return { cache: { data, loading }, request };
}

export default function PersonsListTable({
  persons,
  loading,
  sortBy,
  sortOrder,
  onSortChange,
  selected,
  onToggleSelect,
  onToggleSelectAll,
}: PersonsListTableProps): ReactElement {
  const router = useRouter();
  const [contextMenu, setContextMenu] = useState<RowContextMenu | null>(null);
  const [graphDialog, setGraphDialog] = useState<GraphDialogState | null>(null);

  const connectionsFetch = useLazyPersonFetch<PersonConnection>(
    (id) => `/api/persons/${encodeURIComponent(id)}/connections?connection_type=all&limit=50`,
  );
  const sourcesFetch = useLazyPersonFetch<SourceRecord>(
    (id) => `/api/persons/${encodeURIComponent(id)}/source-records?limit=50`,
  );
  const identifiersFetch = useLazyPersonFetch<PersonIdentifier>(
    (id) => `/api/persons/${encodeURIComponent(id)}/identifiers?limit=50`,
  );

  const allSelected: boolean = persons.length > 0 && persons.every((p) => selected.has(p.person_id));
  const someSelected: boolean = !allSelected && persons.some((p) => selected.has(p.person_id));

  function openPersonNewTab(): void {
    if (!contextMenu) return;
    window.open(`/persons/${contextMenu.personId}`, "_blank");
    setContextMenu(null);
  }

  return (
    <Paper variant="outlined" sx={{ position: "relative" }}>
      {loading ? (
        <LinearProgress sx={{ position: "absolute", top: 0, left: 0, right: 0, zIndex: 1 }} />
      ) : null}
      <Box sx={{ overflow: "auto", maxHeight: "70vh" }}>
      <Table stickyHeader sx={{ opacity: loading ? 0.6 : 1, minWidth: 1600 }}>
        <TableHead sx={{ "& th": { bgcolor: "background.paper" } }}>
          <TableRow>
            <TableCell padding="checkbox" sx={{ position: "sticky", left: 0, zIndex: 5, bgcolor: "background.paper" }}>
              <Checkbox
                size="small"
                indeterminate={someSelected}
                checked={allSelected}
                onChange={onToggleSelectAll}
              />
            </TableCell>
            {COLUMNS.map((col) => (
              <TableCell
                key={col.field}
                align={col.align}
                sx={col.mono ? { fontFamily: "monospace" } : undefined}
              >
                {col.sortable ? (
                  <TableSortLabel
                    active={sortBy === col.field}
                    direction={sortBy === col.field ? sortOrder : "asc"}
                    onClick={() => onSortChange(col.field)}
                  >
                    {col.label}
                  </TableSortLabel>
                ) : (
                  col.label
                )}
              </TableCell>
            ))}
            <TableCell align="center" sx={{ position: "sticky", right: 0, zIndex: 5, bgcolor: "background.paper" }}>Graph</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {persons.length === 0 && !loading ? (
            <TableRow>
              <TableCell colSpan={COLUMNS.length + 2} align="center">
                <Typography variant="body2" color="text.secondary" sx={{ py: 3 }}>
                  No persons match these filters.
                </Typography>
              </TableCell>
            </TableRow>
          ) : null}
          {persons.map((p) => (
            <PersonRow
              key={p.person_id}
              person={p}
              selected={selected.has(p.person_id)}
              onToggleSelect={() => onToggleSelect(p.person_id)}
              onRowClick={() => router.push(`/persons/${p.person_id}`)}
              onContextMenu={(e) => {
                e.preventDefault();
                setContextMenu({ mouseX: e.clientX, mouseY: e.clientY, personId: p.person_id });
              }}
              onOpenGraph={() =>
                setGraphDialog({
                  personId: p.person_id,
                  title: p.preferred_full_name ?? p.person_id,
                })
              }
              connections={connectionsFetch.cache.data[p.person_id]}
              connectionsLoading={connectionsFetch.cache.loading.has(p.person_id)}
              onRequestConnections={() => connectionsFetch.request(p.person_id)}
              sources={sourcesFetch.cache.data[p.person_id]}
              sourcesLoading={sourcesFetch.cache.loading.has(p.person_id)}
              onRequestSources={() => sourcesFetch.request(p.person_id)}
              identifiers={identifiersFetch.cache.data[p.person_id]}
              identifiersLoading={identifiersFetch.cache.loading.has(p.person_id)}
              onRequestIdentifiers={() => identifiersFetch.request(p.person_id)}
            />
          ))}
        </TableBody>
      </Table>
      </Box>
      <Menu
        open={contextMenu !== null}
        onClose={() => setContextMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={
          contextMenu !== null ? { top: contextMenu.mouseY, left: contextMenu.mouseX } : undefined
        }
      >
        <MenuItem onClick={openPersonNewTab}>Open person in new tab</MenuItem>
      </Menu>
      <PersonGraphDialog
        open={graphDialog !== null}
        personId={graphDialog?.personId}
        title={graphDialog?.title ?? ""}
        onClose={() => setGraphDialog(null)}
      />
    </Paper>
  );
}
