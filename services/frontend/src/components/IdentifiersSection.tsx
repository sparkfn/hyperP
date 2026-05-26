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
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import { SourceRecordDetails } from "@/components/SourceRecordDetails";
import type { PersonIdentifier } from "@/lib/api-types-person";
import { formatDate } from "@/lib/display";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

interface Props {
  personId: string;
}

export default function IdentifiersSection({ personId }: Props): ReactElement {
  const [selected, setSelected] = useState<PersonIdentifier | null>(null);
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
              <TableCell>Source record id/s</TableCell>
              <TableCell>Entities</TableCell>
              <TableCell>Last confirmed</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((id) => {
              const sourceRecordRefs: string =
                id.source_record_ids.length > 0 ? id.source_record_ids.join(", ") : "—";
              return (
                <TableRow
                  key={`${id.identifier_type}:${id.normalized_value}`}
                  hover
                  onClick={() => setSelected(id)}
                  sx={{ cursor: "pointer" }}
                >
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
                  <TableCell>
                    <Tooltip title={sourceRecordRefs}>
                      <Typography
                        variant="body2"
                        sx={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {sourceRecordRefs}
                      </Typography>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
                      {id.entities.length > 0 ? (
                        id.entities.map((entity) => (
                          <Chip
                            key={entity.entity_key}
                            label={`${entity.display_name ?? entity.entity_key} (${entity.source_record_count})`}
                            size="small"
                            color="info"
                            variant="outlined"
                          />
                        ))
                      ) : (
                        <Typography variant="body2" color="text.secondary">—</Typography>
                      )}
                    </Stack>
                  </TableCell>
                  <TableCell>{formatDate(id.last_confirmed_at ?? "")}</TableCell>
                </TableRow>
              );
            })}
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
      <IdentifierSourceRecordsDialog identifier={selected} onClose={() => setSelected(null)} />
    </>
  );
}

function IdentifierSourceRecordsDialog({
  identifier,
  onClose,
}: {
  identifier: PersonIdentifier | null;
  onClose: () => void;
}): ReactElement {
  return (
    <Dialog open={identifier !== null} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>
        {identifier !== null
          ? `${identifier.identifier_type}: ${identifier.normalized_value} (${identifier.source_records.length} records)`
          : "Identifier source records"}
      </DialogTitle>
      <DialogContent>
        {identifier === null || identifier.source_records.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No source-record details are available for this identifier.
          </Typography>
        ) : (
          <Stack spacing={1} sx={{ mt: 1 }}>
            {identifier.source_records.map((record, index) => (
              <SourceRecordSection key={record.source_record_pk} index={index} record={record} />
            ))}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}

function SourceRecordSection({
  index,
  record,
}: {
  index: number;
  record: PersonIdentifier["source_records"][number];
}): ReactElement {
  return (
    <Box sx={{ border: "1px solid", borderColor: "divider", borderRadius: 1, p: 1.5 }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
        <Typography variant="subtitle2">{index + 1}. {record.source_record_id}</Typography>
        <Chip label={record.source_system} size="small" />
        <Chip label={record.record_type} size="small" variant="outlined" />
        {record.entity_display_name !== null ? (
          <Chip label={record.entity_display_name} size="small" color="info" variant="outlined" />
        ) : null}
      </Stack>
      <SourceRecordDetails record={record} />
    </Box>
  );
}
