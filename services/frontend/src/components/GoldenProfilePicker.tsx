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

interface Props {
  choices: GoldenProfileChoice[];
  selectedChoiceKeys: Partial<Record<GoldenProfileFieldName, string>>;
  onChange: (fieldName: GoldenProfileFieldName, choiceKey: string) => void;
  disabled: boolean;
}

export default function GoldenProfilePicker({
  choices,
  selectedChoiceKeys,
  onChange,
  disabled,
}: Props): ReactElement {
  return (
    <Stack spacing={2}>
      <Box>
        <Typography variant="subtitle2">Golden profile values</Typography>
        <Typography variant="caption" color="text.secondary">
          Choose the values to keep before confirming the merge.
        </Typography>
      </Box>
      {FIELD_ORDER.map((fieldName) => {
        const fieldChoices = choices.filter((choice) => choice.fieldName === fieldName);
        if (fieldChoices.length === 0) {
          return null;
        }
        return (
          <TextField
            key={fieldName}
            select
            label={goldenProfileFieldLabel(fieldName)}
            value={selectedChoiceKeys[fieldName] ?? ""}
            onChange={(event) => onChange(fieldName, event.target.value)}
            disabled={disabled}
            fullWidth
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
