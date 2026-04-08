"use client";

import { useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";

import { BffError, bffFetch } from "@/lib/api-client";
import type {
  SurvivorshipOverrideRequestBody,
  SurvivorshipOverrideResponseBody,
} from "@/lib/api-types-person";

interface Props {
  open: boolean;
  personId: string;
  onClose: () => void;
  onSaved?: (response: SurvivorshipOverrideResponseBody) => void;
}

const ATTRIBUTE_OPTIONS: readonly string[] = [
  "preferred_phone",
  "preferred_email",
  "preferred_full_name",
  "preferred_dob",
  "preferred_address",
] as const;

export default function SurvivorshipOverrideDialog({
  open,
  personId,
  onClose,
  onSaved,
}: Props): ReactElement {
  const [attributeName, setAttributeName] = useState<string>("preferred_phone");
  const [sourceRecordPk, setSourceRecordPk] = useState<string>("");
  const [reason, setReason] = useState<string>("");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const body: SurvivorshipOverrideRequestBody = {
        attribute_name: attributeName,
        selected_source_record_pk: sourceRecordPk.trim(),
        reason: reason.trim(),
      };
      const result: SurvivorshipOverrideResponseBody =
        await bffFetch<SurvivorshipOverrideResponseBody>(
          `/api/persons/${encodeURIComponent(personId)}/survivorship-overrides`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          },
        );
      if (onSaved) onSaved(result);
      onClose();
    } catch (err: unknown) {
      setError(err instanceof BffError ? err.message : "Override failed.");
    } finally {
      setSubmitting(false);
    }
  };

  const canSubmit: boolean =
    sourceRecordPk.trim().length > 0 && reason.trim().length > 0 && !submitting;

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>Override golden profile field</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error !== null ? <Alert severity="error">{error}</Alert> : null}
          <TextField
            select
            label="Attribute"
            value={attributeName}
            onChange={(e) => setAttributeName(e.target.value)}
            fullWidth
          >
            {ATTRIBUTE_OPTIONS.map((opt) => (
              <MenuItem key={opt} value={opt}>
                {opt}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            label="Source record pk"
            value={sourceRecordPk}
            onChange={(e) => setSourceRecordPk(e.target.value)}
            fullWidth
            required
          />
          <TextField
            label="Reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            multiline
            minRows={2}
            fullWidth
            required
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={handleSubmit} variant="contained" disabled={!canSubmit}>
          Save override
        </Button>
      </DialogActions>
    </Dialog>
  );
}
