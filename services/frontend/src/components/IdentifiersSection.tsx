"use client";

import type { ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import type { PersonIdentifier } from "@/lib/api-types-person";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";
import PaginationBar from "@/components/PaginationBar";

interface Props {
  personId: string;
}

export default function IdentifiersSection({ personId }: Props): ReactElement {
  const { rows, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonIdentifier>(
      `/bff/persons/${encodeURIComponent(personId)}/identifiers`,
    );

  if (error !== null) return <Alert severity="error">{error}</Alert>;
  if (rows === null) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }
  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No identifiers linked to this person.
      </Typography>
    );
  }

  return (
    <>
      <TableContainer>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Type</TableCell>
              <TableCell>Value</TableCell>
              <TableCell>Active</TableCell>
              <TableCell>Verified</TableCell>
              <TableCell>Source system</TableCell>
              <TableCell>Last confirmed</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((id) => (
              <TableRow key={`${id.identifier_type}:${id.normalized_value}`} hover>
                <TableCell>{id.identifier_type}</TableCell>
                <TableCell>
                  <Tooltip title={id.normalized_value}>
                    <Typography
                      variant="body2"
                      fontFamily="monospace"
                      sx={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    >
                      {id.normalized_value}
                    </Typography>
                  </Tooltip>
                </TableCell>
                <TableCell>
                  <Chip
                    label={id.is_active ? "active" : "inactive"}
                    size="small"
                    color={id.is_active ? "success" : "default"}
                  />
                </TableCell>
                <TableCell>
                  <Chip
                    label={id.is_verified ? "verified" : "unverified"}
                    size="small"
                    color={id.is_verified ? "success" : "default"}
                    variant="outlined"
                  />
                </TableCell>
                <TableCell>{id.source_system_key ?? "—"}</TableCell>
                <TableCell>{id.last_confirmed_at?.slice(0, 10) ?? "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
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
  );
}
