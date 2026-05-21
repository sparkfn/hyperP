"use client";

import { useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import UnmergeDialog from "@/components/UnmergeDialog";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";
import type { PersonAuditEvent } from "@/lib/api-types-person";
import { formatDateTime } from "@/lib/display";

interface Props {
  personId: string;
}

function originalMergeEventId(event: PersonAuditEvent): string | null {
  if (event.event_type !== "unmerge") return null;
  const originalId = event.metadata.original_merge_event_id;
  return originalId !== undefined && originalId.length > 0 ? originalId : null;
}

function canUnmerge(event: PersonAuditEvent, reversedMergeEventIds: ReadonlySet<string>): boolean {
  if (event.event_type !== "merge" && event.event_type !== "manual_merge") return false;
  return !reversedMergeEventIds.has(event.merge_event_id);
}

export default function AuditTab({ personId }: Props): ReactElement {
  const { rows: events, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonAuditEvent>(
      `/bff/persons/${encodeURIComponent(personId)}/audit`,
    );
  const [unmergeTarget, setUnmergeTarget] = useState<PersonAuditEvent | null>(null);
  const [optimisticallyUnmergedIds, setOptimisticallyUnmergedIds] = useState<string[]>([]);

  if (error !== null) return <Alert severity="error">{error}</Alert>;
  if (events === null) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }
  if (events.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No audit events for this person.
      </Typography>
    );
  }

  const reversedMergeEventIds = new Set<string>(optimisticallyUnmergedIds);
  for (const event of events) {
    const originalId = originalMergeEventId(event);
    if (originalId !== null) reversedMergeEventIds.add(originalId);
  }

  return (
    <>
      <Paper elevation={0} variant="outlined" sx={{ p: 2 }}>
        <Stack divider={<Divider flexItem />} spacing={2}>
          {events.map((e) => (
            <Box key={e.merge_event_id}>
              <Stack direction="row" justifyContent="space-between" alignItems="flex-start">
                <Box>
                  <Typography variant="subtitle2">{e.event_type}</Typography>
                  <Typography variant="caption" color="text.secondary" display="block">
                    {e.actor_type}:{e.actor_id} · {formatDateTime(e.created_at)}
                  </Typography>
                  {e.reason !== null ? (
                    <Typography variant="body2" sx={{ mt: 0.5 }}>
                      {e.reason}
                    </Typography>
                  ) : null}
                </Box>
                {canUnmerge(e, reversedMergeEventIds) ? (
                  <Button
                    size="small"
                    color="warning"
                    variant="outlined"
                    onClick={() => setUnmergeTarget(e)}
                  >
                    Unmerge
                  </Button>
                ) : null}
              </Stack>
            </Box>
          ))}
        </Stack>
      </Paper>
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
      {unmergeTarget !== null ? (
        <UnmergeDialog
          open={true}
          mergeEventId={unmergeTarget.merge_event_id}
          summary={
            unmergeTarget.absorbed_person_id !== null && unmergeTarget.survivor_person_id !== null
              ? `Restores ${unmergeTarget.absorbed_person_id} from survivor ${unmergeTarget.survivor_person_id}.`
              : undefined
          }
          onClose={() => setUnmergeTarget(null)}
          onSuccess={(mergeEventId) => {
            setOptimisticallyUnmergedIds((current) =>
              current.includes(mergeEventId) ? current : [...current, mergeEventId],
            );
          }}
        />
      ) : null}
    </>
  );
}
