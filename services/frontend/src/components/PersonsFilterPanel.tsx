"use client";

import { useEffect, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";

import type { EntitySummary } from "@/lib/api-types";

export type TriState = "any" | "true" | "false";

export interface PersonsFilters {
  q: string;
  status: string;
  entity_key: string;
  is_high_value: TriState;
  is_high_risk: TriState;
  has_phone: TriState;
  has_email: TriState;
  updated_after: string;
  updated_before: string;
}

export const DEFAULT_FILTERS: PersonsFilters = {
  q: "",
  status: "",
  entity_key: "",
  is_high_value: "any",
  is_high_risk: "any",
  has_phone: "any",
  has_email: "any",
  updated_after: "",
  updated_before: "",
};

interface PersonsFilterPanelProps {
  value: PersonsFilters;
  entities: EntitySummary[];
  onApply: (next: PersonsFilters) => void;
  onClear: () => void;
}

export default function PersonsFilterPanel({
  value,
  entities,
  onApply,
  onClear,
}: PersonsFilterPanelProps): ReactElement {
  const [draft, setDraft] = useState<PersonsFilters>(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  function update<K extends keyof PersonsFilters>(key: K, v: PersonsFilters[K]): void {
    setDraft((prev) => ({ ...prev, [key]: v }));
  }

  function handleApply(): void {
    onApply(draft);
  }

  function handleClear(): void {
    setDraft(DEFAULT_FILTERS);
    onClear();
  }

  return (
    <Paper variant="outlined" sx={{ p: 1.5 }}>
      <Stack spacing={1.25}>
        <Stack direction="row" alignItems="center" spacing={1}>
          <Typography variant="subtitle1">Filters</Typography>
          <Box sx={{ flexGrow: 1 }} />
          <Button onClick={handleClear} color="inherit">
            Clear
          </Button>
          <Button onClick={handleApply} variant="contained">
            Apply
          </Button>
        </Stack>
        <Box
          sx={{
            display: "grid",
            gap: 1,
            alignItems: "end",
            gridTemplateColumns: {
              xs: "repeat(1, 1fr)",
              sm: "repeat(2, 1fr)",
              md: "repeat(3, 1fr)",
              lg: "repeat(4, 1fr)",
              xl: "repeat(6, 1fr)",
            },
          }}
        >
          <TextField
            label="Name contains"
            value={draft.q}
            onChange={(e) => update("q", e.target.value)}
            placeholder="≥3 chars"
            fullWidth
          />
          <TextField
            select
            label="Status"
            value={draft.status}
            onChange={(e) => update("status", e.target.value)}
            fullWidth
          >
            <MenuItem value="">Any</MenuItem>
            <MenuItem value="active">Active</MenuItem>
            <MenuItem value="suppressed">Suppressed</MenuItem>
          </TextField>
          <TextField
            select
            label="Entity"
            value={draft.entity_key}
            onChange={(e) => update("entity_key", e.target.value)}
            fullWidth
          >
            <MenuItem value="">Any</MenuItem>
            {entities.map((ent) => (
              <MenuItem key={ent.entity_key} value={ent.entity_key}>
                {ent.display_name ?? ent.entity_key}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Updated after"
            type="date"
            value={draft.updated_after}
            onChange={(e) => update("updated_after", e.target.value)}
            InputLabelProps={{ shrink: true }}
            fullWidth
          />
          <TextField
            label="Updated before"
            type="date"
            value={draft.updated_before}
            onChange={(e) => update("updated_before", e.target.value)}
            InputLabelProps={{ shrink: true }}
            fullWidth
          />
          <Box />
          <TriStateFilter
            label="High value"
            value={draft.is_high_value}
            onChange={(v) => update("is_high_value", v)}
          />
          <TriStateFilter
            label="High risk"
            value={draft.is_high_risk}
            onChange={(v) => update("is_high_risk", v)}
          />
          <TriStateFilter
            label="Has phone"
            value={draft.has_phone}
            onChange={(v) => update("has_phone", v)}
          />
          <TriStateFilter
            label="Has email"
            value={draft.has_email}
            onChange={(v) => update("has_email", v)}
          />
        </Box>
      </Stack>
    </Paper>
  );
}

interface TriStateFilterProps {
  label: string;
  value: TriState;
  onChange: (v: TriState) => void;
}

function TriStateFilter({ label, value, onChange }: TriStateFilterProps): ReactElement {
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 0.25 }}>
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      <ToggleButtonGroup
        size="small"
        exclusive
        fullWidth
        value={value}
        onChange={(_, v: TriState | null) => onChange(v ?? "any")}
      >
        <ToggleButton value="any" sx={{ py: 0.4, fontSize: "0.72rem" }}>
          Any
        </ToggleButton>
        <ToggleButton value="true" sx={{ py: 0.4, fontSize: "0.72rem" }}>
          Yes
        </ToggleButton>
        <ToggleButton value="false" sx={{ py: 0.4, fontSize: "0.72rem" }}>
          No
        </ToggleButton>
      </ToggleButtonGroup>
    </Box>
  );
}
