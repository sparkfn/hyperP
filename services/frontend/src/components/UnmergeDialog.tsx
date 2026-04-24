"use client";

import { useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { useToast } from "@/components/ToastProvider";
import { BffError, bffFetch } from "@/lib/api-client";
import type { UnmergeRequestBody, UnmergeResponseBody } from "@/lib/api-types-person";

interface Props {
  open: boolean;
  mergeEventId: string;
  summary?: string;
  onClose: () => void;
}

export default function UnmergeDialog({
  open,
  mergeEventId,
  summary,
  onClose,
}: Props): ReactElement {
  const [reason, setReason] = useState<string>("");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  const { showToast } = useToast();

  const handleSubmit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const body: UnmergeRequestBody = {
        merge_event_id: mergeEventId,
        reason: reason.trim(),
      };
      const result: UnmergeResponseBody = await bffFetch<UnmergeResponseBody>("/bff/persons/unmerge", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      showToast(`Unmerged — survivor ${result.survivor_person_id}`, "success");
      onClose();
      setReason("");
      router.refresh();
    } catch (err: unknown) {
      const message: string = err instanceof BffError ? err.message : "Unmerge failed.";
      setError(message);
      showToast(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  const canSubmit: boolean = reason.trim().length > 0 && !submitting;

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>Unmerge</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error !== null ? <Alert severity="error">{error}</Alert> : null}
          <Typography variant="body2" color="text.secondary">
            Reverses the merge recorded by event <code>{mergeEventId}</code>. The absorbed person
            will be restored as a separate Person node. This action is audited.
          </Typography>
          {summary !== undefined ? (
            <Typography variant="caption" color="text.secondary">
              {summary}
            </Typography>
          ) : null}
          <TextField
            label="Reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            multiline
            minRows={2}
            fullWidth
            required
            helperText="Required. Visible in the audit log."
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={handleSubmit} variant="contained" color="warning" disabled={!canSubmit}>
          Unmerge
        </Button>
      </DialogActions>
    </Dialog>
  );
}
