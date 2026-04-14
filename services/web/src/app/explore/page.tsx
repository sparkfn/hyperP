"use client";

import { useCallback, useRef, useState, type FormEvent, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";

import PersonGraphViewer from "@/components/PersonGraphViewer";
import { BffError, bffFetch } from "@/lib/api-client";
import type { Person } from "@/lib/api-types";
import { statusColor } from "@/lib/display";

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

function buildSearchParams(mode: SearchMode, q: string, identifierType: string, value: string): URLSearchParams | string {
  const params = new URLSearchParams();
  if (mode === "freetext") {
    if (q.trim().length < 3) return "Free-text query must be at least 3 characters.";
    params.set("q", q.trim());
    return params;
  }
  if (value.trim().length === 0) return "Identifier value is required.";
  params.set("identifier_type", identifierType);
  params.set("value", value.trim());
  return params;
}

/** A single opened graph panel. */
interface GraphEntry {
  /** Unique key for React. */
  key: number;
  /** Human-readable title for this graph. */
  title: string;
  /** For person-centric graphs. */
  personId?: string;
  /** For generic node graphs (elementId). */
  elementId?: string;
}

export default function ExplorePage(): ReactElement {
  // Search state
  const [mode, setMode] = useState<SearchMode>("freetext");
  const [q, setQ] = useState<string>("");
  const [identifierType, setIdentifierType] = useState<string>("phone");
  const [value, setValue] = useState<string>("");
  const [results, setResults] = useState<Person[] | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // List of opened graph panels (newest first).
  const [graphs, setGraphs] = useState<GraphEntry[]>([]);
  const nextKeyRef = useRef<number>(0);
  // Track the key that should be scrolled into view (only once).
  const scrollTargetRef = useRef<number | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setLoading(true);
    setSearchError(null);
    setResults(null);

    const built = buildSearchParams(mode, q, identifierType, value);
    if (typeof built === "string") {
      setSearchError(built);
      setLoading(false);
      return;
    }

    try {
      const persons = await bffFetch<Person[]>(`/api/persons/search?${built.toString()}`);
      setResults(persons);
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error ? err.message : "Search failed.";
      setSearchError(msg);
    } finally {
      setLoading(false);
    }
  }

  function openPersonGraph(person: Person): void {
    const k = nextKeyRef.current++;
    const entry: GraphEntry = {
      key: k,
      title: person.preferred_full_name ?? person.person_id,
      personId: person.person_id,
    };
    scrollTargetRef.current = k;
    setGraphs((prev) => [entry, ...prev]);
  }

  const handleNavigateNode = useCallback(
    (elementId: string, label: string, displayName: string) => {
      const k = nextKeyRef.current++;
      const entry: GraphEntry = {
        key: k,
        title: `${label}: ${displayName}`,
        elementId,
      };
      scrollTargetRef.current = k;
      setGraphs((prev) => [entry, ...prev]);
    },
    [],
  );

  function closeGraph(key: number): void {
    setGraphs((prev) => prev.filter((g) => g.key !== key));
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Explore Graph
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Search for a person, then explore their relationships interactively.
          Double-click any node to open a new graph from it.
        </Typography>
      </Box>

      <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
        <form onSubmit={onSubmit}>
          <Stack spacing={2}>
            <TextField
              id="explore-search-mode"
              select
              size="small"
              label="Search mode"
              value={mode}
              onChange={(e) => {
                if (isSearchMode(e.target.value)) setMode(e.target.value);
              }}
              slotProps={{ inputLabel: { htmlFor: undefined } }}
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
                autoComplete="off"
                fullWidth
              />
            ) : (
              <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
                <TextField
                  id="explore-identifier-type"
                  select
                  label="Identifier type"
                  value={identifierType}
                  onChange={(e) => setIdentifierType(e.target.value)}
                  slotProps={{ inputLabel: { htmlFor: undefined } }}
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

      {searchError !== null ? <Alert severity="error">{searchError}</Alert> : null}

      {results !== null ? (
        results.length === 0 ? (
          <Alert severity="info">No persons matched.</Alert>
        ) : (
          <Paper elevation={0} variant="outlined">
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Name</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell>Phone</TableCell>
                  <TableCell>Email</TableCell>
                  <TableCell align="right">Sources</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {results.map((p) => (
                  <TableRow
                    key={p.person_id}
                    hover
                    sx={{ cursor: "pointer" }}
                    onClick={() => openPersonGraph(p)}
                  >
                    <TableCell>{p.preferred_full_name ?? p.person_id}</TableCell>
                    <TableCell>
                      <Chip label={p.status} size="small" color={statusColor(p.status)} />
                    </TableCell>
                    <TableCell>{p.preferred_phone ?? "—"}</TableCell>
                    <TableCell>{p.preferred_email ?? "—"}</TableCell>
                    <TableCell align="right">{p.source_record_count}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Paper>
        )
      ) : null}

      {graphs.map((entry) => (
        <Paper
          key={entry.key}
          ref={(el) => {
            if (el && scrollTargetRef.current === entry.key) {
              scrollTargetRef.current = null;
              el.scrollIntoView({ behavior: "smooth", block: "start" });
            }
          }}
          elevation={1}
          variant="outlined"
          sx={{ p: 2 }}
        >
          <Stack spacing={1}>
            <Stack direction="row" alignItems="center" justifyContent="space-between">
              <Typography variant="subtitle1" fontWeight={600}>
                {entry.title}
              </Typography>
              <IconButton size="small" onClick={() => closeGraph(entry.key)}>
                <CloseIcon fontSize="small" />
              </IconButton>
            </Stack>
            <Box sx={{ height: 600 }}>
              <PersonGraphViewer
                personId={entry.personId}
                elementId={entry.elementId}
                onNavigateNode={handleNavigateNode}
              />
            </Box>
          </Stack>
        </Paper>
      ))}
    </Stack>
  );
}
