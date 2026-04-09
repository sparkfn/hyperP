"use client";

import { useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";

import { useToast } from "@/components/ToastProvider";
import { BffError, bffFetch } from "@/lib/api-client";
import type {
  ManualMergeRequestBody,
  ManualMergeResponseBody,
} from "@/lib/api-types-person";

interface Props {
  open: boolean;
  fromPersonId: string;
  onClose: () => void;
  onMerged?: (response: ManualMergeResponseBody) => void;
}

export default function ManualMergeDialog({
  open,
  fromPersonId,
  onClose,
  onMerged,
}: Props): ReactElement {
  const [toPersonId, setToPersonId] = useState<string>("");
  const [reason, setReason] = useState<string>("");
  const [recompute, setRecompute] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  const { showToast } = useToast();

  const handleSubmit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const body: ManualMergeRequestBody = {
        from_person_id: fromPersonId,
        to_person_id: toPersonId.trim(),
        reason: reason.trim(),
        recompute_golden_profile: recompute,
      };
      const result: ManualMergeResponseBody = await bffFetch<ManualMergeResponseBody>(
        "/api/persons/manual-merge",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (onMerged) onMerged(result);
      showToast(`Merged into ${result.to_person_id}`, "success");
      onClose();
      router.refresh();
    } catch (err: unknown) {
      const message: string = err instanceof BffError ? err.message : "Merge failed.";
      setError(message);
      showToast(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  const canSubmit: boolean = toPersonId.trim().length > 0 && reason.trim().length > 0 && !submitting;

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>Merge into another person</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error !== null ? <Alert severity="error">{error}</Alert> : null}
          <TextField
            label="Target person id"
            value={toPersonId}
            onChange={(e) => setToPersonId(e.target.value)}
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
          <FormControlLabel
            control={
              <Checkbox
                checked={recompute}
                onChange={(e) => setRecompute(e.target.checked)}
              />
            }
            label="Recompute golden profile after merge"
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={handleSubmit} variant="contained" disabled={!canSubmit}>
          Merge
        </Button>
      </DialogActions>
    </Dialog>
  );
}
