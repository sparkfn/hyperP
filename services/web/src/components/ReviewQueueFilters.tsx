"use client";

import { useState, type FormEvent, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";

import { QUEUE_STATES } from "@/lib/api-types-ops";

interface Props {
  initialQueueState: string;
  initialAssignedTo: string;
  initialPriorityLte: string;
}

export default function ReviewQueueFilters({
  initialQueueState,
  initialAssignedTo,
  initialPriorityLte,
}: Props): ReactElement {
  const router = useRouter();
  const [queueState, setQueueState] = useState<string>(initialQueueState);
  const [assignedTo, setAssignedTo] = useState<string>(initialAssignedTo);
  const [priorityLte, setPriorityLte] = useState<string>(initialPriorityLte);

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const params = new URLSearchParams();
    if (queueState.length > 0) params.set("queue_state", queueState);
    if (assignedTo.trim().length > 0) params.set("assigned_to", assignedTo.trim());
    if (priorityLte.trim().length > 0) params.set("priority_lte", priorityLte.trim());
    const qs: string = params.toString();
    router.push(qs.length > 0 ? `/review?${qs}` : "/review");
  }

  function onReset(): void {
    setQueueState("");
    setAssignedTo("");
    setPriorityLte("");
    router.push("/review");
  }

  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 2 }}>
      <form onSubmit={onSubmit}>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="flex-end">
          <TextField
            select
            size="small"
            label="Queue state"
            value={queueState}
            onChange={(e) => setQueueState(e.target.value)}
            sx={{ minWidth: 180 }}
          >
            <MenuItem value="">Any</MenuItem>
            {QUEUE_STATES.map((s) => (
              <MenuItem key={s} value={s}>
                {s}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            size="small"
            label="Assigned to"
            value={assignedTo}
            onChange={(e) => setAssignedTo(e.target.value)}
            placeholder="reviewer id"
            sx={{ minWidth: 200 }}
          />
          <TextField
            size="small"
            label="Max priority"
            type="number"
            value={priorityLte}
            onChange={(e) => setPriorityLte(e.target.value)}
            sx={{ maxWidth: 140 }}
          />
          <Box>
            <Stack direction="row" spacing={1}>
              <Button type="submit" variant="contained" size="small">
                Apply
              </Button>
              <Button type="button" onClick={onReset} size="small">
                Reset
              </Button>
            </Stack>
          </Box>
        </Stack>
      </form>
    </Paper>
  );
}
