"use client";

import { useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import { SourceRecordDetails, sourceRecordEntityLabel } from "@/components/SourceRecordDetails";
import type { PersonSourceRecord } from "@/lib/api-types-person";
import { formatDateTime } from "@/lib/display";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

interface Props {
  personId: string;
  onViewInTimeline: (sourceRecordPk: string) => void;
}

export default function SourceRecordsTab({ personId, onViewInTimeline }: Props): ReactElement {
  const [selectedRecord, setSelectedRecord] = useState<PersonSourceRecord | null>(null);
  const { rows, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonSourceRecord>(
      `/bff/persons/${encodeURIComponent(personId)}/source-records`,
    );

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
    <>
      <Paper elevation={0} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Source system</TableCell>
              <TableCell>Entity</TableCell>
              <TableCell>Source record id</TableCell>
              <TableCell>Type</TableCell>
              <TableCell>Link status</TableCell>
              <TableCell>Observed</TableCell>
              <TableCell>Ingested</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((record) => {
              const isConversation: boolean = record.record_type === "conversation";
              return (
                <TableRow
                  key={record.source_record_pk}
                  hover
                  tabIndex={0}
                  onClick={() => setSelectedRecord(record)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setSelectedRecord(record);
                    }
                  }}
                  sx={{ cursor: "pointer" }}
                >
                  <TableCell>{record.source_system}</TableCell>
                  <TableCell>{sourceRecordEntityLabel(record)}</TableCell>
                  <TableCell>{record.source_record_id}</TableCell>
                  <TableCell>
                    <Chip
                      label={record.record_type}
                      size="small"
                      color={isConversation ? "warning" : "default"}
                    />
                  </TableCell>
                  <TableCell>{record.link_status}</TableCell>
                  <TableCell>{formatDateTime(record.observed_at)}</TableCell>
                  <TableCell>{formatDateTime(record.ingested_at)}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Paper>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
        Click a record to view source record details.
      </Typography>
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
      <RecordPayloadDialog
        record={selectedRecord}
        onClose={() => setSelectedRecord(null)}
        onViewInTimeline={onViewInTimeline}
      />
    </>
  );
}

function RecordPayloadDialog({
  record,
  onClose,
  onViewInTimeline,
}: {
  record: PersonSourceRecord | null;
  onClose: () => void;
  onViewInTimeline: (sourceRecordPk: string) => void;
}): ReactElement {
  return (
    <Dialog open={record !== null} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Source record details</DialogTitle>
      <DialogContent dividers>{record !== null ? <SourceRecordDetails record={record} /> : null}</DialogContent>
      <DialogActions>
        {record !== null ? (
          <Button
            onClick={() => {
              onViewInTimeline(record.source_record_pk);
              onClose();
            }}
          >
            View in timeline
          </Button>
        ) : null}
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
