"use client";

import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import {
  goldenProfileFieldLabel,
  type GoldenProfileChoice,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";

const FIELD_ORDER: readonly GoldenProfileFieldName[] = [
  "preferred_full_name",
  "preferred_dob",
  "preferred_phone",
  "preferred_email",
  "preferred_address",
  "preferred_nric",
];

const CHANGED_SX = {
  "& .MuiOutlinedInput-root .MuiOutlinedInput-notchedOutline": { borderColor: "primary.main", borderWidth: 2 },
  "& .MuiOutlinedInput-root:hover .MuiOutlinedInput-notchedOutline": { borderColor: "primary.main", borderWidth: 2 },
  "& .MuiOutlinedInput-root.Mui-focused .MuiOutlinedInput-notchedOutline": { borderColor: "primary.main", borderWidth: 2 },
  "& .MuiInputLabel-root": { color: "primary.main" },
  "& .MuiInputLabel-root.Mui-focused": { color: "primary.main" },
} as const;

interface Props {
  choices: GoldenProfileChoice[];
  selectedChoiceKeys: Partial<Record<GoldenProfileFieldName, string>>;
  originalChoiceKeys?: Partial<Record<GoldenProfileFieldName, string>>;
  readOnlyValues?: Partial<Record<GoldenProfileFieldName, string>>;
  onChange: (fieldName: GoldenProfileFieldName, choiceKey: string) => void;
  disabled: boolean;
  description?: string;
}

export default function GoldenProfilePicker({
  choices,
  selectedChoiceKeys,
  originalChoiceKeys,
  readOnlyValues,
  onChange,
  disabled,
  description = "Choose the values to keep before confirming the merge.",
}: Props): ReactElement {
  return (
    <Stack spacing={2}>
      <Box>
        <Typography variant="subtitle2">Golden profile values</Typography>
        <Typography variant="caption" color="text.secondary">
          {description}
        </Typography>
      </Box>
      {FIELD_ORDER.map((fieldName) => {
        const fieldChoices = choices.filter((choice) => choice.fieldName === fieldName);
        if (fieldChoices.length === 0) {
          const readOnly = readOnlyValues?.[fieldName];
          if (!readOnly) return null;
          return (
            <TextField
              key={fieldName}
              label={goldenProfileFieldLabel(fieldName)}
              value={readOnly}
              disabled
              fullWidth
            />
          );
        }
        const isChanged =
          originalChoiceKeys !== undefined &&
          selectedChoiceKeys[fieldName] !== originalChoiceKeys[fieldName];
        return (
          <TextField
            key={fieldName}
            select
            label={goldenProfileFieldLabel(fieldName)}
            value={selectedChoiceKeys[fieldName] ?? ""}
            onChange={(event) => onChange(fieldName, event.target.value)}
            disabled={disabled}
            fullWidth
            sx={isChanged ? CHANGED_SX : undefined}
          >
            {fieldChoices.map((choice) => (
              <MenuItem key={choice.key} value={choice.key}>
                <Stack spacing={0.25}>
                  <Typography variant="body2">{choice.value}</Typography>
                  <Typography variant="caption" color="text.secondary">
                    {choice.sourceLabel}
                    {choice.isSurvivorDefault ? " · survivor default" : ""}
                  </Typography>
                </Stack>
              </MenuItem>
            ))}
          </TextField>
        );
      })}
    </Stack>
  );
}
