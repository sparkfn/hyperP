"use client";

import { useEffect, useRef, useState, type ReactElement } from "react";

import AddIcon from "@mui/icons-material/Add";
import ClearIcon from "@mui/icons-material/Clear";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Collapse from "@mui/material/Collapse";
import Divider from "@mui/material/Divider";
import FormControl from "@mui/material/FormControl";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Select, { type SelectChangeEvent } from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import type { EntitySummary, SourceSystemSummary } from "@/lib/api-types";
import DatePickerField from "@/components/DatePickerField";
import PersonsFilterAddressSection from "@/components/PersonsFilterAddressSection";
import PersonsFilterDobSection from "@/components/PersonsFilterDobSection";

export interface PersonsFilters {
  q: string;
  entity_keys: string[];
  source_keys: string[];
  has_bankruptcy_case: "" | "true" | "false";
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
  entity_keys: [],
  source_keys: [],
  has_bankruptcy_case: "",
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
    f.has_bankruptcy_case !== "" ||
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

// Non-text inputs trigger an immediate fetch on change
const IMMEDIATE_KEYS = new Set<keyof PersonsFilters>([
  "entity_keys",
  "source_keys",
  "has_bankruptcy_case",
  "has_address",
  "has_dob",
  "updated_after",
  "updated_before",
  "dob_from",
  "dob_to",
]);

const DEBOUNCE_MS = 400;

// onChange signature used by sub-sections; `immediate` forces an instant apply
export interface FilterChangeHandler {
  (patch: Partial<PersonsFilters>, immediate?: boolean): void;
}

interface PersonsFilterPanelProps {
  value: PersonsFilters;
  entities: EntitySummary[];
  sourceSystems: SourceSystemSummary[];
  onApply: (next: PersonsFilters) => void;
  onClear: () => void;
}

function asPresenceFilter(value: string): "" | "true" | "false" {
  return value === "true" || value === "false" ? value : "";
}

export default function PersonsFilterPanel({
  value,
  entities,
  sourceSystems,
  onApply,
  onClear,
}: PersonsFilterPanelProps): ReactElement {
  const [draft, setDraft] = useState<PersonsFilters>(value);
  const [expanded, setExpanded] = useState(() => hasAdvancedFilters(value));

  // Refs hold latest values so timer callbacks never see stale closures.
  // Updated via effects to satisfy the "no ref access during render" lint rule.
  const onApplyRef = useRef(onApply);
  const draftRef = useRef(draft);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    onApplyRef.current = onApply;
  }, [onApply]);

  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  // Sync external value changes into draft (e.g. parent clear-all resets value)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDraft(value);
  }, [value]);

  // Debounce text-field changes — fires DEBOUNCE_MS after the last keystroke.
  // Refs are safe inside the timer callback and excluded from the deps array.
  useEffect(() => {
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => {
      debounceTimer.current = null;
      onApplyRef.current(draftRef.current);
    }, DEBOUNCE_MS);
    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
    };
  }, [draft.q, draft.addr_street, draft.addr_unit, draft.addr_city, draft.addr_postal, draft.addr_country]);

  function handleChange(update: Partial<PersonsFilters>, immediate = false): void {
    const hasImmediateKey = (Object.keys(update) as (keyof PersonsFilters)[]).some((k) =>
      IMMEDIATE_KEYS.has(k),
    );
    if (hasImmediateKey || immediate) {
      // Cancel any pending debounce so we don't fire a stale text-only apply
      if (debounceTimer.current) {
        clearTimeout(debounceTimer.current);
        debounceTimer.current = null;
      }
      const next = { ...draft, ...update };
      setDraft(next);
      onApplyRef.current(next);
    } else {
      setDraft((prev) => ({ ...prev, ...update }));
    }
  }

  function handleClear(): void {
    if (debounceTimer.current) {
      clearTimeout(debounceTimer.current);
      debounceTimer.current = null;
    }
    setDraft(DEFAULT_FILTERS);
    onClear();
  }

  function handleMultiSelectChange(
    field: "entity_keys" | "source_keys",
    event: SelectChangeEvent<string[]>,
  ): void {
    const value = event.target.value;
    handleChange({ [field]: typeof value === "string" ? value.split(",") : value });
  }

  function entityLabel(entityKey: string): string {
    return entities.find((ent) => ent.entity_key === entityKey)?.display_name ?? entityKey;
  }

  function sourceLabel(sourceKey: string): string {
    return sourceSystems.find((source) => source.source_key === sourceKey)?.display_name ?? sourceKey;
  }

  const hasActiveFilters: boolean =
    draft.q.trim().length > 0 || draft.entity_keys.length > 0 || draft.source_keys.length > 0 || hasAdvancedFilters(draft);

  return (
    <Paper variant="outlined" sx={{ px: 2, py: 1.5 }}>
      {/* Primary row: search + entity */}
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: expanded ? 1.5 : 0 }}>
        <TextField
          size="small"
          placeholder="Search by name, NRIC, email or phone..."
          value={draft.q}
          onChange={(e) => handleChange({ q: e.target.value })}
          sx={{ flex: 1, minWidth: 240 }}
          InputProps={{
            sx: { fontSize: "0.875rem" },
            endAdornment: draft.q ? (
              <InputAdornment position="end">
                <IconButton size="small" edge="end" onClick={() => handleChange({ q: "" }, true)}>
                  <ClearIcon sx={{ fontSize: "1rem" }} />
                </IconButton>
              </InputAdornment>
            ) : null,
          }}
        />
        <FormControl size="small" sx={{ width: 220, maxWidth: 220 }}>
          <InputLabel>Entity</InputLabel>
          <Select
            multiple
            label="Entity"
            value={draft.entity_keys}
            onChange={(e) => handleMultiSelectChange("entity_keys", e)}
            renderValue={(selected) => (
              <Box sx={{ display: "flex", flexWrap: "nowrap", gap: 0.5, overflow: "hidden" }}>
                {selected[0] ? <Chip label={entityLabel(selected[0])} size="small" sx={{ maxWidth: 140 }} /> : null}
                {selected.length > 1 ? <Chip label={`+${selected.length - 1}`} size="small" /> : null}
              </Box>
            )}
            endAdornment={
              draft.entity_keys.length > 0 ? (
                <InputAdornment position="end" sx={{ mr: 2 }}>
                  <IconButton size="small" onClick={() => handleChange({ entity_keys: [] })}>
                    <ClearIcon sx={{ fontSize: "1rem" }} />
                  </IconButton>
                </InputAdornment>
              ) : null
            }
            sx={{ fontSize: "0.875rem" }}
          >
            {entities.map((ent) => (
              <MenuItem key={ent.entity_key} value={ent.entity_key}>
                {ent.display_name ?? ent.entity_key}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ width: 220, maxWidth: 220 }}>
          <InputLabel>Source</InputLabel>
          <Select
            multiple
            label="Source"
            value={draft.source_keys}
            onChange={(e) => handleMultiSelectChange("source_keys", e)}
            renderValue={(selected) => (
              <Box sx={{ display: "flex", flexWrap: "nowrap", gap: 0.5, overflow: "hidden" }}>
                {selected[0] ? <Chip label={sourceLabel(selected[0])} size="small" sx={{ maxWidth: 140 }} /> : null}
                {selected.length > 1 ? <Chip label={`+${selected.length - 1}`} size="small" /> : null}
              </Box>
            )}
            endAdornment={
              draft.source_keys.length > 0 ? (
                <InputAdornment position="end" sx={{ mr: 2 }}>
                  <IconButton size="small" onClick={() => handleChange({ source_keys: [] })}>
                    <ClearIcon sx={{ fontSize: "1rem" }} />
                  </IconButton>
                </InputAdornment>
              ) : null
            }
            sx={{ fontSize: "0.875rem" }}
          >
            {sourceSystems.map((source) => (
              <MenuItem key={source.source_key} value={source.source_key}>
                {source.display_name ?? source.source_key}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 170 }}>
          <InputLabel>Bankruptcy</InputLabel>
          <Select
            label="Bankruptcy"
            value={draft.has_bankruptcy_case}
            onChange={(e) => handleChange({ has_bankruptcy_case: asPresenceFilter(e.target.value) })}
            sx={{ fontSize: "0.875rem" }}
          >
            <MenuItem value="">All</MenuItem>
            <MenuItem value="true">Has bankruptcy</MenuItem>
            <MenuItem value="false">No bankruptcy</MenuItem>
          </Select>
        </FormControl>
        {hasActiveFilters && (
          <Button size="small" color="inherit" onClick={handleClear} sx={{ whiteSpace: "nowrap" }}>
            Clear all
          </Button>
        )}
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
        <PersonsFilterAddressSection filters={draft} onChange={handleChange} />
        <PersonsFilterDobSection filters={draft} onChange={handleChange} />

        <Typography variant="caption" color="text.secondary" sx={{ mt: 2, mb: 1, display: "block", px: 0.5 }}>
          LAST UPDATED
        </Typography>
        <Box sx={{ display: "grid", gap: 1.5, gridTemplateColumns: { xs: "repeat(2, 1fr)", md: "repeat(4, 1fr)" } }}>
          <DatePickerField
            label="After"
            value={draft.updated_after}
            onChange={(v) => handleChange({ updated_after: v })}
          />
          <DatePickerField
            label="Before"
            value={draft.updated_before}
            onChange={(v) => handleChange({ updated_before: v })}
          />
        </Box>
      </Collapse>
    </Paper>
  );
}
