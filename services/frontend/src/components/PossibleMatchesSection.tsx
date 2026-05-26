"use client";

import { useEffect, useState, type KeyboardEvent, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import PossibleMatchDetailDialog from "@/components/PossibleMatchDetailDialog";
import type { PersonSharedIdentifierCandidate } from "@/lib/api-types-person";
import { formatDob } from "@/lib/display";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

interface Props {
  personId: string;
  personName: string | null;
  refreshKey?: number;
  onMerged?: () => void;
}

export default function PossibleMatchesSection({
  personId,
  personName,
  refreshKey = 0,
  onMerged,
}: Props): ReactElement {
  const [selectedCandidate, setSelectedCandidate] = useState<PersonSharedIdentifierCandidate | null>(null);
  const { rows, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev, refresh } =
    usePaginatedFetch<PersonSharedIdentifierCandidate>(
      `/bff/persons/${encodeURIComponent(personId)}/shared-identifiers`,
    );

  useEffect(() => {
    if (refreshKey > 0) refresh();
  }, [refresh, refreshKey]);

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
        No other active persons share identifiers with this person.
      </Typography>
    );
  }

  return (
    <>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Name</TableCell>
            <TableCell>Status</TableCell>
            <TableCell>Shared identifiers</TableCell>
            <TableCell>Profile completion</TableCell>
            <TableCell>Contact</TableCell>
            <TableCell>DOB</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((candidate) => {
            const displayName = candidate.preferred_full_name ?? "Unnamed person";
            return (
              <TableRow
                key={candidate.person_id}
                hover
                role="button"
                tabIndex={0}
                onClick={() => setSelectedCandidate(candidate)}
                onKeyDown={(event: KeyboardEvent<HTMLTableRowElement>) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setSelectedCandidate(candidate);
                  }
                }}
                sx={{ cursor: "pointer" }}
              >
                <TableCell>{displayName}</TableCell>
                <TableCell>{candidate.status}</TableCell>
                <TableCell>
                  <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                    <Chip
                      label={candidate.identifier_strength}
                      color={candidate.identifier_strength === "strong" ? "error" : "warning"}
                      size="small"
                      variant="outlined"
                    />
                    {candidate.identifiers.map((identifier) => (
                      <Chip
                        key={`${identifier.identifier_type}:${identifier.normalized_value}`}
                        label={`${identifier.identifier_type}: ${identifier.normalized_value}`}
                        size="small"
                      />
                    ))}
                  </Stack>
                </TableCell>
                <TableCell>{formatProfileCompleteness(candidate.profile_completeness_score)}</TableCell>
                <TableCell>{candidate.preferred_email ?? candidate.preferred_phone ?? "—"}</TableCell>
                <TableCell>{formatDob(candidate.preferred_dob)}</TableCell>
              </TableRow>
            );
          })}
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
      <PossibleMatchDetailDialog
        personId={personId}
        personName={personName}
        candidate={selectedCandidate}
        onClose={() => setSelectedCandidate(null)}
        onMerged={() => {
          setSelectedCandidate(null);
          if (onMerged !== undefined) onMerged();
          else refresh();
        }}
      />
    </>
  );
}

function formatProfileCompleteness(score: number): string {
  return `${(score * 100).toFixed(0)}%`;
}
