"use client";

import type { ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import type { PersonConnection } from "@/lib/api-types";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";
import PaginationBar from "@/components/PaginationBar";

interface Props {
  personId: string;
}

export default function ConnectionsCard({ personId }: Props): ReactElement {
  const { rows: connections, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonConnection>(
      `/api/persons/${encodeURIComponent(personId)}/connections?connection_type=all`,
    );

  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Connections
      </Typography>
      {error !== null ? (
        <Alert severity="error">{error}</Alert>
      ) : connections === null ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
          <CircularProgress size={24} />
        </Box>
      ) : connections.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No connected persons via shared identifiers, addresses, or relationships.
        </Typography>
      ) : (
        <>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Name</TableCell>
                <TableCell>Status</TableCell>
                <TableCell align="right">Hops</TableCell>
                <TableCell>Shared identifiers</TableCell>
                <TableCell>Shared addresses</TableCell>
                <TableCell>Relationships</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {connections.map((c) => (
                <TableRow key={c.person_id} hover>
                  <TableCell>
                    <Link href={`/persons/${c.person_id}`} style={{ textDecoration: "none" }}>
                      {c.preferred_full_name ?? c.person_id}
                    </Link>
                  </TableCell>
                  <TableCell>{c.status}</TableCell>
                  <TableCell align="right">{c.hops}</TableCell>
                  <TableCell>
                    {c.shared_identifiers
                      .map((s) => `${s.identifier_type}:${s.normalized_value}`)
                      .join(", ") || "—"}
                  </TableCell>
                  <TableCell>
                    {c.shared_addresses.map((a) => a.normalized_full ?? a.address_id).join(", ") ||
                      "—"}
                  </TableCell>
                  <TableCell>
                    {c.knows_relationships
                      .map((k) => k.relationship_label ?? k.relationship_category)
                      .join(", ") || "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <PaginationBar
            from={from}
            to={to}
            total={total}
            hasPrev={hasPrev}
            hasNext={hasNext}
            loading={loading}
            onPrev={goPrev}
            onNext={goNext}
          />
        </>
      )}
    </Paper>
  );
}
