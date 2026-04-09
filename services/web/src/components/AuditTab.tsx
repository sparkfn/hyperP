"use client";

import { useEffect, useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import UnmergeDialog from "@/components/UnmergeDialog";
import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonAuditEvent } from "@/lib/api-types-person";

interface Props {
  personId: string;
}

export default function AuditTab({ personId }: Props): ReactElement {
  const [events, setEvents] = useState<PersonAuditEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unmergeTarget, setUnmergeTarget] = useState<PersonAuditEvent | null>(null);

  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      try {
        const data: PersonAuditEvent[] = await bffFetch<PersonAuditEvent[]>(
          `/api/persons/${encodeURIComponent(personId)}/audit`,
        );
        if (!cancelled) setEvents(data);
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof BffError ? err.message : "Failed to load audit events.");
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [personId]);

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
                    {e.actor_type}:{e.actor_id} · {e.created_at}
                  </Typography>
                  {e.reason !== null ? (
                    <Typography variant="body2" sx={{ mt: 0.5 }}>
                      {e.reason}
                    </Typography>
                  ) : null}
                </Box>
                {e.event_type === "merge" ? (
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
        />
      ) : null}
    </>
  );
}
