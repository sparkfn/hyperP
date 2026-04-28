"use client";

import { useEffect, useState, type ReactElement } from "react";

import AddIcon from "@mui/icons-material/Add";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Collapse from "@mui/material/Collapse";
import Divider from "@mui/material/Divider";
import FormControl from "@mui/material/FormControl";
import IconButton from "@mui/material/IconButton";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Select from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import type { EntitySummary } from "@/lib/api-types";
import DatePickerField from "@/components/DatePickerField";
import PersonsFilterAddressSection from "@/components/PersonsFilterAddressSection";
import PersonsFilterDobSection from "@/components/PersonsFilterDobSection";

export interface PersonsFilters {
  q: string;
  entity_key: string;
  has_address: "" | "true" | "false";
  addr_street: string;
  addr_unit: string;
  addr_city: string;
  addr_postal: string;
  addr_country: string;
  updated_after: string;
  updated_before: string;
  has_dob: "" | "true" | "false";
  dob_from: string;
  dob_to: string;
}

export const DEFAULT_FILTERS: PersonsFilters = {
  q: "",
  entity_key: "",
  has_address: "",
  addr_street: "",
  addr_unit: "",
  addr_city: "",
  addr_postal: "",
  addr_country: "",
  updated_after: "",
  updated_before: "",
  has_dob: "",
  dob_from: "",
  dob_to: "",
};

function hasAdvancedFilters(f: PersonsFilters): boolean {
  return (
    f.has_address !== "" ||
    f.addr_street !== "" ||
    f.addr_unit !== "" ||
    f.addr_city !== "" ||
    f.addr_postal !== "" ||
    f.addr_country !== "" ||
    f.updated_after !== "" ||
    f.updated_before !== "" ||
    f.has_dob !== "" ||
    f.dob_from !== "" ||
    f.dob_to !== ""
  );
}

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
  const [expanded, setExpanded] = useState(() => hasAdvancedFilters(value));

  useEffect(() => {
    setDraft(value);
  }, [value]);

  function patch(update: Partial<PersonsFilters>): void {
    setDraft((prev) => ({ ...prev, ...update }));
  }

  function handleApply(): void {
    onApply(draft);
  }

  function handleClear(): void {
    setDraft(DEFAULT_FILTERS);
    onClear();
  }

  const hasActiveFilters: boolean =
    draft.q.trim().length > 0 || draft.entity_key !== "" || hasAdvancedFilters(draft);

  return (
    <Paper variant="outlined" sx={{ px: 2, py: 1.5 }}>
      {/* Primary row: search + entity + quick apply */}
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: expanded ? 1.5 : 0 }}>
        <TextField
          size="small"
          placeholder="Search by name, NRIC, email or phone..."
          value={draft.q}
          onChange={(e) => patch({ q: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === "Enter") handleApply();
          }}
          sx={{ flex: 1, minWidth: 240 }}
          InputProps={{ sx: { fontSize: "0.875rem" } }}
        />
        <FormControl size="small" sx={{ minWidth: 180 }}>
          <InputLabel>Entity</InputLabel>
          <Select
            label="Entity"
            value={draft.entity_key}
            onChange={(e) => patch({ entity_key: e.target.value })}
            sx={{ fontSize: "0.875rem" }}
          >
            <MenuItem value="">All entities</MenuItem>
            {entities.map((ent) => (
              <MenuItem key={ent.entity_key} value={ent.entity_key}>
                {ent.display_name ?? ent.entity_key}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        {hasActiveFilters && (
          <Button size="small" color="inherit" onClick={handleClear} sx={{ whiteSpace: "nowrap" }}>
            Clear all
          </Button>
        )}
        <Button size="small" variant="contained" onClick={handleApply}>
          Search
        </Button>
        <Divider orientation="vertical" flexItem />
        <Button
          size="small"
          color="inherit"
          onClick={() => setExpanded((v) => !v)}
          endIcon={
            <IconButton size="small" sx={{ p: 0 }}>
              <AddIcon
                sx={{
                  fontSize: "1rem",
                  transform: expanded ? "rotate(45deg)" : "none",
                  transition: "transform 0.2s",
                }}
              />
            </IconButton>
          }
        >
          {expanded ? "Fewer filters" : "More filters"}
        </Button>
      </Stack>

      {/* Collapsible advanced filters */}
      <Collapse in={expanded}>
        <Divider sx={{ my: 1 }} />
        <PersonsFilterAddressSection filters={draft} onChange={patch} />
        <PersonsFilterDobSection filters={draft} onChange={patch} />

        <Typography variant="caption" color="text.secondary" sx={{ mt: 2, mb: 1, display: "block", px: 0.5 }}>
          LAST UPDATED
        </Typography>
        <Box sx={{ display: "grid", gap: 1.5, gridTemplateColumns: { xs: "repeat(2, 1fr)", md: "repeat(4, 1fr)" } }}>
          <DatePickerField
            label="After"
            value={draft.updated_after}
            onChange={(v) => patch({ updated_after: v })}
          />
          <DatePickerField
            label="Before"
            value={draft.updated_before}
            onChange={(v) => patch({ updated_before: v })}
          />
        </Box>
      </Collapse>
    </Paper>
  );
}
