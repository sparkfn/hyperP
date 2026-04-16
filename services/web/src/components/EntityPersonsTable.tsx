"use client";

import { useState, type MouseEvent, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Chip from "@mui/material/Chip";
import IconButton from "@mui/material/IconButton";
import LinearProgress from "@mui/material/LinearProgress";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Tooltip from "@mui/material/Tooltip";
import AccountTreeIcon from "@mui/icons-material/AccountTree";

import type { EntityPerson } from "@/lib/api-types";
import { confidenceColor, statusColor } from "@/lib/display";

export type SortField =
  | "preferred_full_name"
  | "status"
  | "preferred_phone"
  | "preferred_email"
  | "source_record_count"
  | "connection_count"
  | "phone_confidence";

export type SortOrder = "asc" | "desc";

interface EntityPersonsTableProps {
  persons: EntityPerson[];
  totalCount: number;
  page: number;
  rowsPerPage: number;
  sortBy: SortField;
  sortOrder: SortOrder;
  loading: boolean;
  onSortChange: (field: SortField) => void;
  onPageChange: (page: number) => void;
  onRowsPerPageChange: (rowsPerPage: number) => void;
}

interface RowContextMenu {
  mouseX: number;
  mouseY: number;
  person: EntityPerson;
}

interface ColumnDef {
  field: SortField;
  label: string;
  align?: "left" | "right" | "center";
}

const COLUMNS: readonly ColumnDef[] = [
  { field: "preferred_full_name", label: "Name" },
  { field: "status", label: "Status" },
  { field: "preferred_phone", label: "Phone" },
  { field: "phone_confidence", label: "Confidence", align: "right" },
  { field: "preferred_email", label: "Email" },
  { field: "source_record_count", label: "Sources", align: "right" },
  { field: "connection_count", label: "Connections", align: "right" },
] as const;

function openGraphInNewTab(person: EntityPerson): void {
  const params = new URLSearchParams({ person_id: person.person_id });
  if (person.preferred_full_name) params.set("name", person.preferred_full_name);
  window.open(`/graph?${params.toString()}`, "_blank");
}

export default function EntityPersonsTable({
  persons, totalCount, page, rowsPerPage,
  sortBy, sortOrder, loading, onSortChange, onPageChange, onRowsPerPageChange,
}: EntityPersonsTableProps): ReactElement {
  const router = useRouter();
  const [contextMenu, setContextMenu] = useState<RowContextMenu | null>(null);

  function handleRowClick(personId: string): void {
    router.push(`/persons/${personId}`);
  }

  function handleContextMenu(event: MouseEvent<HTMLTableRowElement>, person: EntityPerson): void {
    event.preventDefault();
    setContextMenu({ mouseX: event.clientX, mouseY: event.clientY, person });
  }

  function openPersonNewTab(): void {
    if (!contextMenu) return;
    window.open(`/persons/${contextMenu.person.person_id}`, "_blank");
    setContextMenu(null);
  }

  function openGraphFromMenu(): void {
    if (!contextMenu) return;
    openGraphInNewTab(contextMenu.person);
    setContextMenu(null);
  }

  return (
    <Paper elevation={0} variant="outlined" sx={{ position: "relative" }}>
      {loading ? <LinearProgress sx={{ position: "absolute", top: 0, left: 0, right: 0 }} /> : null}
      <Table size="small" sx={{ opacity: loading ? 0.5 : 1 }}>
        <TableHead>
          <TableRow>
            {COLUMNS.map((col) => (
              <TableCell key={col.field} align={col.align}>
                <TableSortLabel
                  active={sortBy === col.field}
                  direction={sortBy === col.field ? sortOrder : "asc"}
                  onClick={() => onSortChange(col.field)}
                >
                  {col.label}
                </TableSortLabel>
              </TableCell>
            ))}
            <TableCell align="center">Graph</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {persons.map((p) => (
            <TableRow
              key={p.person_id}
              hover
              sx={{ cursor: "pointer" }}
              onClick={() => handleRowClick(p.person_id)}
              onContextMenu={(e) => handleContextMenu(e, p)}
            >
              <TableCell>{p.preferred_full_name ?? p.person_id}</TableCell>
              <TableCell>
                <Chip label={p.status} size="small" color={statusColor(p.status)} />
              </TableCell>
              <TableCell>{p.preferred_phone ?? "\u2014"}</TableCell>
              <TableCell align="right">
                {p.phone_confidence !== null ? (
                  <Tooltip title={`Phone confidence: ${(p.phone_confidence * 100).toFixed(0)}%`}>
                    <Chip
                      label={p.phone_confidence.toFixed(1)}
                      size="small"
                      color={confidenceColor(p.phone_confidence)}
                      variant="outlined"
                      sx={{ fontSize: "0.75rem", minWidth: 40 }}
                    />
                  </Tooltip>
                ) : "\u2014"}
              </TableCell>
              <TableCell>{p.preferred_email ?? "\u2014"}</TableCell>
              <TableCell align="right">{p.source_record_count}</TableCell>
              <TableCell align="right">{p.connection_count}</TableCell>
              <TableCell align="center">
                <Tooltip title="Open graph in new tab">
                  <IconButton
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      openGraphInNewTab(p);
                    }}
                  >
                    <AccountTreeIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <TablePagination
        component="div"
        count={totalCount}
        page={page}
        rowsPerPage={rowsPerPage}
        onPageChange={(_, newPage) => onPageChange(newPage)}
        onRowsPerPageChange={(e) => onRowsPerPageChange(parseInt(e.target.value, 10))}
        rowsPerPageOptions={[10, 20, 50]}
      />
      <Menu
        open={contextMenu !== null}
        onClose={() => setContextMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={
          contextMenu !== null ? { top: contextMenu.mouseY, left: contextMenu.mouseX } : undefined
        }
      >
        <MenuItem onClick={openPersonNewTab}>Open person in new tab</MenuItem>
        <MenuItem onClick={openGraphFromMenu}>Open graph in new tab</MenuItem>
      </Menu>
    </Paper>
  );
}
