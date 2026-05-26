"use client";

import { useEffect, useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Grid from "@mui/material/Grid2";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import Gate from "@/components/auth/Gate";
import ManualMergeDialog from "@/components/ManualMergeDialog";
import { SourceRecordDetails } from "@/components/SourceRecordDetails";
import { BffError, bffFetch } from "@/lib/api-client";
import type { ManualMergeResponseBody, PersonSharedIdentifierCandidate, PossibleMatchDetail } from "@/lib/api-types-person";

interface Props {
  personId: string;
  personName: string | null;
  candidate: PersonSharedIdentifierCandidate | null;
  onClose: () => void;
  onMerged?: (response: ManualMergeResponseBody) => void;
}

export default function PossibleMatchDetailDialog({
  personId,
  personName,
  candidate,
  onClose,
  onMerged,
}: Props): ReactElement {
  const [detail, setDetail] = useState<PossibleMatchDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [mergeOpen, setMergeOpen] = useState<boolean>(false);

  useEffect(() => {
    if (candidate === null) {
      queueMicrotask(() => {
        setDetail(null);
        setError(null);
      });
      return;
    }
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setLoading(true);
      setError(null);
    });
    void bffFetch<PossibleMatchDetail>(
      `/bff/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidate.person_id)}/detail`,
    )
      .then((nextDetail) => {
        if (!cancelled) setDetail(nextDetail);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof BffError ? err.message : "Could not load possible match details.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [candidate, personId]);

  const candidateName = candidate?.preferred_full_name ?? detail?.candidate_name ?? "Candidate";
  const currentName = personName ?? "Current person";

  return (
    <>
      <Dialog open={candidate !== null} onClose={onClose} fullWidth maxWidth="xl">
        <DialogTitle>{candidateName} vs {currentName}</DialogTitle>
        <DialogContent>
          {loading ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
              <CircularProgress size={24} />
            </Box>
          ) : null}
          {error !== null ? <Alert severity="error">{error}</Alert> : null}
          {detail !== null ? (
            <Stack spacing={2} sx={{ mt: 1 }}>
              {detail.shared_identifier_groups.map((group) => (
                <Box key={`${group.identifier_type}:${group.normalized_value}`} sx={{ border: "1px solid", borderColor: "divider", borderRadius: 1, p: 1.5 }}>
                  <Typography variant="subtitle2" sx={{ mb: 1 }}>
                    {group.identifier_type}: {group.normalized_value}
                  </Typography>
                  <Grid container spacing={2}>
                    <Grid size={{ xs: 12, md: 6 }}>
                      <Typography variant="caption" color="text.secondary">{candidateName}</Typography>
                      <SourceRecordList records={group.candidate_source_records} />
                    </Grid>
                    <Grid size={{ xs: 12, md: 6 }}>
                      <Typography variant="caption" color="text.secondary">{currentName}</Typography>
                      <SourceRecordList records={group.current_person_source_records} />
                    </Grid>
                  </Grid>
                </Box>
              ))}
            </Stack>
          ) : null}
        </DialogContent>
        <DialogActions>
          <Gate mode="admin">
            <Button variant="contained" onClick={() => setMergeOpen(true)} disabled={candidate === null}>
              Merge
            </Button>
          </Gate>
          <Button onClick={onClose}>Close</Button>
        </DialogActions>
      </Dialog>
      {candidate !== null ? (
        <ManualMergeDialog
          open={mergeOpen}
          fromPersonId={candidate.person_id}
          defaultToPersonId={personId}
          lockTarget
          title={`Merge ${candidateName} into ${currentName}`}
          onClose={() => setMergeOpen(false)}
          onMerged={(response) => {
            setMergeOpen(false);
            onClose();
            if (onMerged !== undefined) onMerged(response);
          }}
        />
      ) : null}
    </>
  );
}

function SourceRecordList({ records }: { records: PossibleMatchDetail["shared_identifier_groups"][number]["candidate_source_records"] }): ReactElement {
  if (records.length === 0) {
    return <Typography variant="body2" color="text.secondary">No source records.</Typography>;
  }
  return (
    <Stack spacing={1} sx={{ mt: 1 }}>
      {records.map((record, index) => (
        <Box key={record.source_record_pk} sx={{ border: "1px solid", borderColor: "divider", borderRadius: 1, p: 1 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>{index + 1}. {record.source_record_id}</Typography>
          <SourceRecordDetails record={record} />
        </Box>
      ))}
    </Stack>
  );
}
