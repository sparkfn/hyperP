"use client";

import React, { useState, type FormEvent, type ReactElement } from "react";
import Link from "next/link";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
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

import type { Profile } from "@/lib/api-types-person";
import { formatDate } from "@/lib/display";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

const STATUS_OPTIONS = [
  { value: "open", label: "Open" },
  { value: "promoted", label: "Promoted" },
  { value: "retired", label: "Retired" },
  { value: "all", label: "All statuses" },
] as const;

type StatusFilter = (typeof STATUS_OPTIONS)[number]["value"];

function isStatusFilter(value: string): value is StatusFilter {
  return STATUS_OPTIONS.some((option) => option.value === value);
}

function profilesBasePath(query: string, status: StatusFilter): string {
  const params = new URLSearchParams();
  if (query.length >= 2) params.set("q", query);
  if (status !== "all") params.set("status", status);
  const queryString = params.toString();
  return queryString ? `/bff/profiles?${queryString}` : "/bff/profiles";
}

function ProfilesTable({ basePath }: { basePath: string }): ReactElement {
  const page = usePaginatedFetch<Profile>(basePath);
  const rows = page.rows ?? [];

  return (
    <Paper variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Profile</TableCell>
            <TableCell>Status</TableCell>
            <TableCell align="right">Records</TableCell>
            <TableCell align="right">Identifiers</TableCell>
            <TableCell>Materialized</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((profile) => (
            <TableRow key={profile.profile_id} hover>
              <TableCell>
                <Link href={`/profiles/${encodeURIComponent(profile.profile_id)}`}>
                  {profile.profile_id}
                </Link>
              </TableCell>
              <TableCell>
                <Stack direction="row" spacing={1}>
                  <Chip label={profile.status} size="small" />
                  <Chip label="Unverified" size="small" color="warning" />
                </Stack>
              </TableCell>
              <TableCell align="right">{profile.record_count}</TableCell>
              <TableCell align="right">{profile.identifier_count}</TableCell>
              <TableCell>{formatDate(profile.materialized_at)}</TableCell>
            </TableRow>
          ))}
          {rows.length === 0 ? (
            <TableRow>
              <TableCell colSpan={5}>
                {page.loading ? "Loading profiles…" : (page.error ?? "No profiles found.")}
              </TableCell>
            </TableRow>
          ) : null}
        </TableBody>
      </Table>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ p: 1 }}>
        <Button size="small" disabled={!page.hasPrev} onClick={page.goPrev}>
          Previous
        </Button>
        <Button size="small" disabled={!page.hasNext} onClick={page.goNext}>
          Next
        </Button>
        <Typography variant="caption">
          {page.from}–{page.to}
          {page.total !== null ? ` of ${page.total}` : ""}
        </Typography>
      </Stack>
    </Paper>
  );
}

export default function ProfilesPage(): ReactElement {
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("open");
  const basePath = profilesBasePath(query, status);

  const submitSearch = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setQuery(queryInput.trim());
  };

  return (
    <Stack spacing={2} sx={{ p: 3 }}>
      <Typography variant="h5">Profiles</Typography>
      <form onSubmit={submitSearch}>
        <Stack direction="row" spacing={2}>
          <TextField
            size="small"
            label="Search"
            value={queryInput}
            onChange={(event) => setQueryInput(event.target.value)}
            helperText="Minimum 2 characters"
          />
          <TextField
            size="small"
            select
            label="Status"
            value={status}
            onChange={(event) => {
              const next = event.target.value;
              if (isStatusFilter(next)) setStatus(next);
            }}
          >
            {STATUS_OPTIONS.map((option) => (
              <MenuItem key={option.value} value={option.value}>
                {option.label}
              </MenuItem>
            ))}
          </TextField>
          <Button type="submit" variant="outlined">
            Search
          </Button>
        </Stack>
      </form>
      <ProfilesTable key={basePath} basePath={basePath} />
    </Stack>
  );
}
