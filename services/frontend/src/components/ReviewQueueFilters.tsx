"use client";

import { useState, type FormEvent, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";

import { QUEUE_STATES } from "@/lib/api-types-ops";

export interface ReviewFilterValues {
  queue_state: string;
  resolved: string;
  assigned_to: string;
  person_id: string;
  q: string;
  decision: string;
  engine_type: string;
  priority_gte: string;
  priority_lte: string;
  confidence_gte: string;
  confidence_lte: string;
  created_after: string;
  created_before: string;
  sla_due_after: string;
  sla_due_before: string;
  overdue_sla: boolean;
  sort_by: string;
  sort_order: string;
  limit: string;
}

interface Props {
  initial: ReviewFilterValues;
}

const DECISION_OPTIONS: readonly string[] = ["merge", "review", "no_match"];
const ENGINE_OPTIONS: readonly string[] = ["deterministic", "heuristic", "pair_audit", "llm", "manual"];
const SORT_OPTIONS: readonly { value: string; label: string }[] = [
  { value: "priority", label: "Priority" },
  { value: "confidence", label: "Confidence" },
  { value: "sla_due_at", label: "SLA due" },
  { value: "created_at", label: "Created" },
  { value: "updated_at", label: "Updated" },
  { value: "queue_state", label: "Queue state" },
];

const dateProps = { slotProps: { inputLabel: { shrink: true } } } as const;

export default function ReviewQueueFilters({ initial }: Props): ReactElement {
  const router = useRouter();
  const [v, setV] = useState<ReviewFilterValues>(initial);

  function set<K extends keyof ReviewFilterValues>(key: K, value: ReviewFilterValues[K]): void {
    setV((prev) => ({ ...prev, [key]: value }));
  }

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const params = new URLSearchParams();
    // String filters — only set when non-empty (cursor is intentionally dropped → page 1).
    const stringKeys: (keyof ReviewFilterValues)[] = [
      "queue_state", "resolved", "assigned_to", "person_id", "q", "decision", "engine_type",
      "priority_gte", "priority_lte", "confidence_gte", "confidence_lte",
      "created_after", "created_before", "sla_due_after", "sla_due_before",
      "sort_by", "sort_order", "limit",
    ];
    for (const key of stringKeys) {
      const value = String(v[key]).trim();
      if (value.length > 0) params.set(key, value);
    }
    if (v.overdue_sla) params.set("overdue_sla", "true");
    const qs: string = params.toString();
    router.push(qs.length > 0 ? `/review?${qs}` : "/review");
  }

  function onReset(): void {
    router.push("/review");
  }

  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 2 }}>
      <form onSubmit={onSubmit}>
        <Stack spacing={2}>
          {/* Search + primary filters */}
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="flex-end" flexWrap="wrap" useFlexGap>
            <TextField
              size="small"
              label="Search"
              value={v.q}
              onChange={(e) => set("q", e.target.value)}
              placeholder="case id, decision, person…"
              sx={{ minWidth: 240, flex: 1 }}
            />
            <TextField
              select
              size="small"
              label="Queue state"
              value={v.queue_state}
              onChange={(e) => set("queue_state", e.target.value)}
              sx={{ minWidth: 160 }}
            >
              <MenuItem value="">Any</MenuItem>
              {QUEUE_STATES.map((s) => (
                <MenuItem key={s} value={s}>{s}</MenuItem>
              ))}
            </TextField>
            <TextField
              select
              size="small"
              label="Resolution"
              value={v.resolved}
              onChange={(e) => set("resolved", e.target.value)}
              sx={{ minWidth: 140 }}
            >
              <MenuItem value="">Any</MenuItem>
              <MenuItem value="false">Unresolved</MenuItem>
              <MenuItem value="true">Resolved</MenuItem>
            </TextField>
            <TextField
              size="small"
              label="Assigned to"
              value={v.assigned_to}
              onChange={(e) => set("assigned_to", e.target.value)}
              placeholder="reviewer id"
              sx={{ minWidth: 160 }}
            />
            <TextField
              size="small"
              label="Person ID"
              value={v.person_id}
              onChange={(e) => set("person_id", e.target.value)}
              placeholder="person id"
              sx={{ minWidth: 160 }}
            />
            <TextField
              select
              size="small"
              label="Decision"
              value={v.decision}
              onChange={(e) => set("decision", e.target.value)}
              sx={{ minWidth: 140 }}
            >
              <MenuItem value="">Any</MenuItem>
              {DECISION_OPTIONS.map((d) => <MenuItem key={d} value={d}>{d}</MenuItem>)}
            </TextField>
            <TextField
              select
              size="small"
              label="Engine"
              value={v.engine_type}
              onChange={(e) => set("engine_type", e.target.value)}
              sx={{ minWidth: 140 }}
            >
              <MenuItem value="">Any</MenuItem>
              {ENGINE_OPTIONS.map((e2) => <MenuItem key={e2} value={e2}>{e2}</MenuItem>)}
            </TextField>
          </Stack>

          {/* Ranges + sort */}
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="flex-end" flexWrap="wrap" useFlexGap>
            <TextField size="small" label="Priority ≥" type="number" value={v.priority_gte}
              onChange={(e) => set("priority_gte", e.target.value)} sx={{ maxWidth: 110 }} />
            <TextField size="small" label="Priority ≤" type="number" value={v.priority_lte}
              onChange={(e) => set("priority_lte", e.target.value)} sx={{ maxWidth: 110 }} />
            <TextField size="small" label="Confidence ≥" type="number" value={v.confidence_gte}
              onChange={(e) => set("confidence_gte", e.target.value)} inputProps={{ min: 0, max: 1, step: 0.05 }} sx={{ maxWidth: 130 }} />
            <TextField size="small" label="Confidence ≤" type="number" value={v.confidence_lte}
              onChange={(e) => set("confidence_lte", e.target.value)} inputProps={{ min: 0, max: 1, step: 0.05 }} sx={{ maxWidth: 130 }} />
            <TextField select size="small" label="Sort by" value={v.sort_by}
              onChange={(e) => set("sort_by", e.target.value)} sx={{ minWidth: 150 }}>
              <MenuItem value="">Default</MenuItem>
              {SORT_OPTIONS.map((o) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
            </TextField>
            <TextField select size="small" label="Order" value={v.sort_order || "asc"}
              onChange={(e) => set("sort_order", e.target.value)} sx={{ minWidth: 110 }}>
              <MenuItem value="asc">Asc</MenuItem>
              <MenuItem value="desc">Desc</MenuItem>
            </TextField>
          </Stack>

          {/* Date ranges + overdue */}
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="flex-end" flexWrap="wrap" useFlexGap>
            <TextField size="small" label="Created after" type="date" value={v.created_after}
              onChange={(e) => set("created_after", e.target.value)} {...dateProps} />
            <TextField size="small" label="Created before" type="date" value={v.created_before}
              onChange={(e) => set("created_before", e.target.value)} {...dateProps} />
            <TextField size="small" label="SLA due after" type="date" value={v.sla_due_after}
              onChange={(e) => set("sla_due_after", e.target.value)} {...dateProps} />
            <TextField size="small" label="SLA due before" type="date" value={v.sla_due_before}
              onChange={(e) => set("sla_due_before", e.target.value)} {...dateProps} />
            <FormControlLabel
              control={<Checkbox size="small" checked={v.overdue_sla} onChange={(e) => set("overdue_sla", e.target.checked)} />}
              label="Overdue SLA"
            />
            <Box sx={{ ml: { sm: "auto" } }}>
              <Stack direction="row" spacing={1}>
                <Button type="submit" variant="contained" size="small">Apply</Button>
                <Button type="button" onClick={onReset} size="small">Reset</Button>
              </Stack>
            </Box>
          </Stack>
        </Stack>
      </form>
    </Paper>
  );
}
