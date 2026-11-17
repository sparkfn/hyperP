"use client";

import type { ReactElement } from "react";
import Link from "next/link";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import type { UnlinkedProfile } from "@/lib/api-types-person";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

const UNLINKED_BASE_PATH = "/bff/profiles/unlinked";

function UnlinkedProfilesTable(): ReactElement {
  const page = usePaginatedFetch<UnlinkedProfile>(UNLINKED_BASE_PATH);
  const rows = page.rows ?? [];

  return (
    <Paper variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Profile</TableCell>
            <TableCell>Status</TableCell>
            <TableCell align="right">Value score</TableCell>
            <TableCell align="right">Records</TableCell>
            <TableCell>Attach NRIC</TableCell>
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
              <TableCell align="right">{profile.value_score}</TableCell>
              <TableCell align="right">{profile.record_count}</TableCell>
              <TableCell>
                <Link href={`/profiles/${encodeURIComponent(profile.profile_id)}`}>
                  Attach NRIC
                </Link>
              </TableCell>
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

export default function UnlinkedProfilesPage(): ReactElement {
  return (
    <Stack spacing={2} sx={{ p: 3 }}>
      <Typography variant="h5">Unlinked profiles</Typography>
      <Typography variant="body2" color="text.secondary">
        Open profiles without an NRIC anchor, most CRM evidence first.
      </Typography>
      <UnlinkedProfilesTable />
    </Stack>
  );
}
