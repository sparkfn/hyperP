"use client";

import { useEffect, useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonMatchDecision } from "@/lib/api-types-person";

interface Props {
  personId: string;
}

type ChipColor = "success" | "warning" | "error" | "default";

function decisionColor(decision: string): ChipColor {
  if (decision === "merge") return "success";
  if (decision === "review") return "warning";
  if (decision === "no_match") return "error";
  return "default";
}

export default function MatchesTab({ personId }: Props): ReactElement {
  const [rows, setRows] = useState<PersonMatchDecision[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      try {
        const data: PersonMatchDecision[] = await bffFetch<PersonMatchDecision[]>(
          `/api/persons/${encodeURIComponent(personId)}/matches`,
        );
        if (!cancelled) setRows(data);
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof BffError ? err.message : "Failed to load matches.");
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [personId]);

  if (error !== null) return <Alert severity="error">{error}</Alert>;
  if (rows === null) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }
  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No match decisions recorded for this person.
      </Typography>
    );
  }

  return (
    <Paper elevation={0} variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Engine</TableCell>
            <TableCell>Decision</TableCell>
            <TableCell align="right">Confidence</TableCell>
            <TableCell>Reasons</TableCell>
            <TableCell>Created</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((m) => (
            <TableRow key={m.match_decision_id} hover>
              <TableCell>{m.engine_type}</TableCell>
              <TableCell>
                <Chip label={m.decision} size="small" color={decisionColor(m.decision)} />
              </TableCell>
              <TableCell align="right">{m.confidence.toFixed(2)}</TableCell>
              <TableCell>{m.reasons.join(", ") || "—"}</TableCell>
              <TableCell>{m.created_at}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Paper>
  );
}
