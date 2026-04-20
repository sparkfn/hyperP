"use client";

import { useState, type FormEvent, type MouseEvent, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AccountTreeIcon from "@mui/icons-material/AccountTree";

import { BffError, bffFetch } from "@/lib/api-client";
import type { Person } from "@/lib/api-types";
import { statusColor } from "@/lib/display";
import PersonGraphDialog from "@/components/PersonGraphDialog";

type SearchMode = "freetext" | "identifier";

const SEARCH_MODES: readonly SearchMode[] = ["freetext", "identifier"] as const;
const IDENTIFIER_TYPES: readonly string[] = [
  "phone",
  "email",
  "government_id_hash",
  "external_customer_id",
  "membership_id",
  "crm_contact_id",
  "loyalty_id",
] as const;

function isSearchMode(value: string): value is SearchMode {
  return (SEARCH_MODES as readonly string[]).includes(value);
}

interface SearchInput {
  mode: SearchMode;
  q: string;
  identifierType: string;
  value: string;
}

function buildSearchParams(input: SearchInput): URLSearchParams | string {
  const params = new URLSearchParams();
  if (input.mode === "freetext") {
    if (input.q.trim().length < 3) {
      return "Free-text query must be at least 3 characters.";
    }
    params.set("q", input.q.trim());
    return params;
  }
  if (input.value.trim().length === 0) {
    return "Identifier value is required.";
  }
  params.set("identifier_type", input.identifierType);
  params.set("value", input.value.trim());
  return params;
}

function openGraphInNewTab(person: Person): void {
  const params = new URLSearchParams({ person_id: person.person_id });
  if (person.preferred_full_name) params.set("name", person.preferred_full_name);
  window.open(`/graph?${params.toString()}`, "_blank");
}

export default function HomePage(): ReactElement {
  const [mode, setMode] = useState<SearchMode>("freetext");
  const [q, setQ] = useState<string>("");
  const [identifierType, setIdentifierType] = useState<string>("phone");
  const [value, setValue] = useState<string>("");
  const [results, setResults] = useState<Person[] | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setLoading(true);
    setError(null);
    setResults(null);

    const built: URLSearchParams | string = buildSearchParams({ mode, q, identifierType, value });
    if (typeof built === "string") {
      setError(built);
      setLoading(false);
      return;
    }

    try {
      const persons: Person[] = await bffFetch<Person[]>(`/api/persons/search?${built.toString()}`);
      setResults(persons);
    } catch (err: unknown) {
      const message: string = err instanceof BffError || err instanceof Error ? err.message : "Search failed.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Person Search
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Look up unified persons by identifier or free-text name.
        </Typography>
      </Box>

      <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
        <form onSubmit={onSubmit}>
          <Stack spacing={2}>
            <TextField
              select
              size="small"
              label="Search mode"
              value={mode}
              onChange={(e) => {
                if (isSearchMode(e.target.value)) setMode(e.target.value);
              }}
              sx={{ maxWidth: 240 }}
            >
              <MenuItem value="freetext">Free-text (name)</MenuItem>
              <MenuItem value="identifier">Identifier (exact)</MenuItem>
            </TextField>

            {mode === "freetext" ? (
              <TextField
                label="Name"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="e.g. Jane Tan"
                fullWidth
              />
            ) : (
              <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
                <TextField
                  select
                  label="Identifier type"
                  value={identifierType}
                  onChange={(e) => setIdentifierType(e.target.value)}
                  sx={{ minWidth: 220 }}
                >
                  {IDENTIFIER_TYPES.map((t) => (
                    <MenuItem key={t} value={t}>
                      {t}
                    </MenuItem>
                  ))}
                </TextField>
                <TextField
                  label="Value"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  placeholder="normalized value"
                  fullWidth
                />
              </Stack>
            )}

            <Box>
              <Button type="submit" variant="contained" disabled={loading}>
                {loading ? <CircularProgress size={20} /> : "Search"}
              </Button>
            </Box>
          </Stack>
        </form>
      </Paper>

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      {results !== null ? <ResultsTable persons={results} /> : null}
    </Stack>
  );
}

interface ResultsTableProps {
  persons: Person[];
}

interface RowContextMenu {
  mouseX: number;
  mouseY: number;
  person: Person;
}

interface GraphDialogState {
  personId: string;
  title: string;
}

function ResultsTable({ persons }: ResultsTableProps): ReactElement {
  const router = useRouter();
  const [contextMenu, setContextMenu] = useState<RowContextMenu | null>(null);
  const [graphDialog, setGraphDialog] = useState<GraphDialogState | null>(null);

  if (persons.length === 0) {
    return <Alert severity="info">No persons matched.</Alert>;
  }

  function openGraphDialog(person: Person): void {
    setGraphDialog({
      personId: person.person_id,
      title: person.preferred_full_name ?? person.person_id,
    });
  }

  function handleRowClick(personId: string): void {
    router.push(`/persons/${personId}`);
  }

  function handleContextMenu(event: MouseEvent<HTMLTableRowElement>, person: Person): void {
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
    <>
      <Paper elevation={0} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Name</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>Phone</TableCell>
              <TableCell>Email</TableCell>
              <TableCell align="right">Sources</TableCell>
              <TableCell align="right">Connections</TableCell>
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
                <TableCell>{p.preferred_phone ?? "—"}</TableCell>
                <TableCell>{p.preferred_email ?? "—"}</TableCell>
                <TableCell align="right">{p.source_record_count}</TableCell>
                <TableCell align="right">{p.connection_count}</TableCell>
                <TableCell align="center">
                  <Tooltip title="Open graph">
                    <IconButton
                      size="small"
                      onClick={(e) => {
                        e.stopPropagation();
                        openGraphDialog(p);
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
      </Paper>
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
      <PersonGraphDialog
        open={graphDialog !== null}
        personId={graphDialog?.personId}
        title={graphDialog?.title ?? ""}
        onClose={() => setGraphDialog(null)}
      />
    </>
  );
}
