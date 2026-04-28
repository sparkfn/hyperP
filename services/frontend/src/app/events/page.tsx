"use client";

import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { DownstreamEvent } from "@/lib/api-types-ops";

function toIsoOrEmpty(localInput: string): string {
  if (localInput.trim().length === 0) return "";
  const d: Date = new Date(localInput);
  if (Number.isNaN(d.getTime())) return "";
  return d.toISOString();
}

export default function EventsPage(): ReactElement {
  const [sinceInput, setSinceInput] = useState<string>("");
  const [events, setEvents] = useState<DownstreamEvent[] | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  async function load(since: string): Promise<void> {
    setLoading(true);
    setError(null);
    try {
      const iso: string = toIsoOrEmpty(since);
      const path: string =
        iso.length > 0 ? `/bff/events?since=${encodeURIComponent(iso)}` : "/bff/events";
      const res: DownstreamEvent[] = await bffFetch<DownstreamEvent[]>(path);
      setEvents(res);
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error ? err.message : "Failed to load events.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSubmit(e: FormEvent<HTMLFormElement>): void {
    e.preventDefault();
    void load(sinceInput);
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Downstream events
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Poll the /v1/events feed for person lifecycle events.
        </Typography>
      </Box>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <form onSubmit={onSubmit}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="flex-end">
            <TextField
              label="Since"
              type="datetime-local"
              size="small"
              value={sinceInput}
              onChange={(e) => setSinceInput(e.target.value)}
              InputLabelProps={{ shrink: true }}
            />
            <Button type="submit" variant="contained" disabled={loading}>
              {loading ? <CircularProgress size={20} /> : "Fetch"}
            </Button>
          </Stack>
        </form>
      </Paper>

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      {events !== null ? <EventList events={events} /> : null}
    </Stack>
  );
}

interface EventListProps {
  events: DownstreamEvent[];
}

function EventList({ events }: EventListProps): ReactElement {
  if (events.length === 0) {
    return <Alert severity="info">No events.</Alert>;
  }
  return (
    <Stack spacing={2}>
      {events.map((ev) => (
        <Paper key={ev.event_id} variant="outlined" sx={{ p: 2 }}>
          <Stack spacing={1}>
            <Stack direction="row" spacing={1} alignItems="center">
              <Chip label={ev.event_type} size="small" color="primary" />
              <Typography variant="caption" color="text.secondary">
                {ev.event_id}
              </Typography>
            </Stack>
            <Typography variant="caption" color="text.secondary">
              {ev.created_at}
            </Typography>
            {ev.affected_person_ids.length > 0 ? (
              <Stack direction="row" spacing={1} flexWrap="wrap">
                {ev.affected_person_ids.map((pid) => (
                  <Link key={pid} href={`/persons/${pid}`}>
                    {pid}
                  </Link>
                ))}
              </Stack>
            ) : null}
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1,
                backgroundColor: "#f5f5f5",
                fontSize: 12,
                overflowX: "auto",
                whiteSpace: "pre-wrap",
              }}
            >
              {JSON.stringify(ev.metadata, null, 2)}
            </Box>
          </Stack>
        </Paper>
      ))}
    </Stack>
  );
}
