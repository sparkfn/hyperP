"use client";

import { useState, type FormEvent, type ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
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

function ResultsTable({ persons }: ResultsTableProps): ReactElement {
  if (persons.length === 0) {
    return <Alert severity="info">No persons matched.</Alert>;
  }
  return (
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
          {persons.map((p) => (
            <TableRow key={p.person_id} hover>
              <TableCell>
                <Link href={`/persons/${p.person_id}`} style={{ textDecoration: "none" }}>
                  {p.preferred_full_name ?? p.person_id}
                </Link>
              </TableCell>
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
  );
}
