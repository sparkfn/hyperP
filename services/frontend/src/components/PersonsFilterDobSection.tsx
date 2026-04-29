"use client";

import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";

import DatePickerField from "@/components/DatePickerField";
import type { FilterChangeHandler, PersonsFilters } from "@/components/PersonsFilterPanel";

const TOGGLE_SX = {
  flexShrink: 0,
  "& .MuiToggleButton-root": {
    py: "1px",
    px: 1,
    fontSize: "0.75rem",
    lineHeight: 1.66,
    textTransform: "none",
  },
} as const;

interface DobSectionProps {
  filters: Pick<PersonsFilters, "has_dob" | "dob_from" | "dob_to">;
  onChange: FilterChangeHandler;
}

export default function PersonsFilterDobSection({
  filters,
  onChange,
}: DobSectionProps): ReactElement {
  const disabled = filters.has_dob === "false";

  return (
    <>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 2 }}>
        <Typography variant="caption" color="text.secondary" sx={{ px: 0.5, whiteSpace: "nowrap" }}>
          DATE OF BIRTH
        </Typography>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={filters.has_dob || ""}
          onChange={(_e, next: "" | "true" | "false" | null) => {
            const val = next ?? "";
            if (val === "false") {
              onChange({ has_dob: "false", dob_from: "", dob_to: "" });
            } else {
              onChange({ has_dob: val });
            }
          }}
          sx={TOGGLE_SX}
        >
          <ToggleButton value="">Any</ToggleButton>
          <ToggleButton value="true">Has DOB</ToggleButton>
          <ToggleButton value="false">No DOB</ToggleButton>
        </ToggleButtonGroup>
      </Stack>
      <Box
        sx={{
          display: "grid",
          gap: 1.5,
          mt: 1,
          gridTemplateColumns: { xs: "repeat(2, 1fr)", md: "repeat(4, 1fr)" },
        }}
      >
        <DatePickerField
          label="From"
          value={filters.dob_from}
          onChange={(v) => onChange({ dob_from: v })}
          disabled={disabled}
        />
        <DatePickerField
          label="To"
          value={filters.dob_to}
          onChange={(v) => onChange({ dob_to: v })}
          disabled={disabled}
        />
      </Box>
    </>
  );
}