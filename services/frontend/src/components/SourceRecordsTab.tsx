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
import type { PersonSourceRecord } from "@/lib/api-types-person";

interface Props {
  personId: string;
}

export default function SourceRecordsTab({ personId }: Props): ReactElement {
  const [rows, setRows] = useState<PersonSourceRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      try {
        const data: PersonSourceRecord[] = await bffFetch<PersonSourceRecord[]>(
          `/api/persons/${encodeURIComponent(personId)}/source-records`,
        );
        if (!cancelled) setRows(data);
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof BffError ? err.message : "Failed to load source records.");
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
        No source records linked to this person.
      </Typography>
    );
  }

  return (
    <Paper elevation={0} variant="outlined">
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Source system</TableCell>
            <TableCell>Source record id</TableCell>
            <TableCell>Type</TableCell>
            <TableCell>Link status</TableCell>
            <TableCell>Observed</TableCell>
            <TableCell>Ingested</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((r) => {
            const isConversation: boolean = r.record_type === "conversation";
            return (
              <TableRow
                key={r.source_record_pk}
                hover
                sx={isConversation ? { bgcolor: "warning.light", opacity: 0.95 } : undefined}
              >
                <TableCell>{r.source_system}</TableCell>
                <TableCell>{r.source_record_id}</TableCell>
                <TableCell>
                  <Chip
                    label={r.record_type}
                    size="small"
                    color={isConversation ? "warning" : "default"}
                  />
                </TableCell>
                <TableCell>{r.link_status}</TableCell>
                <TableCell>{r.observed_at}</TableCell>
                <TableCell>{r.ingested_at}</TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </Paper>
  );
}
