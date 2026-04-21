"use client";

import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import DeleteIcon from "@mui/icons-material/Delete";

import type { ReportParamType } from "@/lib/api-types";

const PARAM_TYPES: readonly ReportParamType[] = [
  "string",
  "integer",
  "float",
  "date",
  "boolean",
] as const;

export interface EditableParam {
  clientId: string;
  name: string;
  label: string;
  param_type: ReportParamType;
  required: boolean;
  default_value: string;
}

interface ReportParamRowProps {
  param: EditableParam;
  onChange: (field: keyof EditableParam, value: string | boolean) => void;
  onRemove: () => void;
}

export default function ReportParamRow({
  param,
  onChange,
  onRemove,
}: ReportParamRowProps): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack spacing={1.5}>
        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems="flex-start">
          <TextField
            label="Name"
            value={param.name}
            onChange={(e) => onChange("name", e.target.value)}
            required
            size="small"
            placeholder="param_name"
            sx={{ minWidth: 160 }}
          />
          <TextField
            label="Label"
            value={param.label}
            onChange={(e) => onChange("label", e.target.value)}
            required
            size="small"
            placeholder="Display label"
            sx={{ flexGrow: 1, minWidth: 160 }}
          />
          <TextField
            select
            label="Type"
            value={param.param_type}
            onChange={(e) => onChange("param_type", e.target.value)}
            size="small"
            sx={{ minWidth: 120 }}
          >
            {PARAM_TYPES.map((t) => (
              <MenuItem key={t} value={t}>
                {t}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Default"
            value={param.default_value}
            onChange={(e) => onChange("default_value", e.target.value)}
            size="small"
            placeholder="optional"
            sx={{ minWidth: 120 }}
          />
          <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
            <FormControlLabel
              control={
                <Checkbox
                  checked={param.required}
                  onChange={(e) => onChange("required", e.target.checked)}
                  size="small"
                />
              }
              label="Required"
              slotProps={{ typography: { variant: "body2" } }}
            />
            <Tooltip title="Remove parameter">
              <IconButton size="small" color="error" onClick={onRemove}>
                <DeleteIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Box>
        </Stack>
      </Stack>
    </Paper>
  );
}
