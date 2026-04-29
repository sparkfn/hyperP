"use client";

import type { ReactElement } from "react";

import ClearIcon from "@mui/icons-material/Clear";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";

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

interface AddressSectionProps {
  filters: Pick<
    PersonsFilters,
    "has_address" | "addr_street" | "addr_unit" | "addr_city" | "addr_postal" | "addr_country"
  >;
  onChange: FilterChangeHandler;
}

function clearAdornment(value: string, onClear: () => void): ReactElement | null {
  if (!value) return null;
  return (
    <InputAdornment position="end">
      <IconButton size="small" edge="end" onClick={onClear}>
        <ClearIcon sx={{ fontSize: "1rem" }} />
      </IconButton>
    </InputAdornment>
  );
}

export default function PersonsFilterAddressSection({
  filters,
  onChange,
}: AddressSectionProps): ReactElement {
  const disabled = filters.has_address === "false";

  return (
    <>
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="caption" color="text.secondary" sx={{ px: 0.5, whiteSpace: "nowrap" }}>
          ADDRESS
        </Typography>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={filters.has_address || ""}
          onChange={(_e, next: "" | "true" | "false" | null) => {
            const val = next ?? "";
            if (val === "false") {
              onChange({
                has_address: "false",
                addr_street: "",
                addr_unit: "",
                addr_city: "",
                addr_postal: "",
                addr_country: "",
              });
            } else {
              onChange({ has_address: val });
            }
          }}
          sx={TOGGLE_SX}
        >
          <ToggleButton value="">Any</ToggleButton>
          <ToggleButton value="true">Has Address</ToggleButton>
          <ToggleButton value="false">No Address</ToggleButton>
        </ToggleButtonGroup>
      </Stack>
      <Box
        sx={{
          display: "grid",
          gap: 1.5,
          mt: 1,
          alignItems: "center",
          gridTemplateColumns: {
            xs: "repeat(2, 1fr)",
            sm: "repeat(3, 1fr)",
            md: "repeat(5, 1fr)",
            lg: "repeat(6, 1fr)",
          },
        }}
      >
        <TextField
          size="small"
          label="Street / road"
          value={filters.addr_street}
          onChange={(e) => onChange({ addr_street: e.target.value })}
          placeholder="Partial match"
          disabled={disabled}
          InputProps={{
            endAdornment: clearAdornment(filters.addr_street, () => onChange({ addr_street: "" }, true)),
          }}
        />
        <TextField
          size="small"
          label="Unit / block"
          value={filters.addr_unit}
          onChange={(e) => onChange({ addr_unit: e.target.value })}
          placeholder="Partial match"
          disabled={disabled}
          InputProps={{
            endAdornment: clearAdornment(filters.addr_unit, () => onChange({ addr_unit: "" }, true)),
          }}
        />
        <TextField
          size="small"
          label="City / town"
          value={filters.addr_city}
          onChange={(e) => onChange({ addr_city: e.target.value })}
          placeholder="Partial match"
          disabled={disabled}
          InputProps={{
            endAdornment: clearAdornment(filters.addr_city, () => onChange({ addr_city: "" }, true)),
          }}
        />
        <TextField
          size="small"
          label="Postal / ZIP"
          value={filters.addr_postal}
          onChange={(e) => onChange({ addr_postal: e.target.value })}
          placeholder="Partial match"
          disabled={disabled}
          InputProps={{
            endAdornment: clearAdornment(filters.addr_postal, () => onChange({ addr_postal: "" }, true)),
          }}
        />
        <TextField
          size="small"
          label="Country"
          value={filters.addr_country}
          onChange={(e) => onChange({ addr_country: e.target.value })}
          placeholder="e.g. SG"
          disabled={disabled}
          InputProps={{
            endAdornment: clearAdornment(filters.addr_country, () => onChange({ addr_country: "" }, true)),
          }}
        />
      </Box>
    </>
  );
}
