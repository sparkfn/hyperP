"use client";

import { useEffect, useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";

import GoldenProfilePicker from "@/components/GoldenProfilePicker";
import { useToast } from "@/components/ToastProvider";
import { BffError, bffFetch } from "@/lib/api-client";
import type {
  ManualMergeRequestBody,
  ManualMergeResponseBody,
} from "@/lib/api-types-person";
import {
  buildGoldenProfileChoices,
  defaultChoiceByField,
  loadGoldenProfileEvidence,
  selectionBody,
  type GoldenProfileChoice,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";

interface Props {
  open: boolean;
  fromPersonId: string;
  onClose: () => void;
  onMerged?: (response: ManualMergeResponseBody) => void;
  defaultToPersonId?: string;
  lockTarget?: boolean;
  title?: string;
}

export default function ManualMergeDialog({
  open,
  fromPersonId,
  onClose,
  onMerged,
  defaultToPersonId,
  lockTarget = false,
  title = "Merge into another person",
}: Props): ReactElement {
  const [toPersonId, setToPersonId] = useState<string>(defaultToPersonId ?? "");
  const [reason, setReason] = useState<string>("");
  const [recompute, setRecompute] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [loadingChoices, setLoadingChoices] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [choices, setChoices] = useState<GoldenProfileChoice[]>([]);
  const [selectedChoiceKeys, setSelectedChoiceKeys] = useState<
    Partial<Record<GoldenProfileFieldName, string>>
  >({});
  const router = useRouter();
  const { showToast } = useToast();

  const targetPersonId = toPersonId.trim();

  useEffect(() => {
    if (!open || targetPersonId.length === 0 || targetPersonId === fromPersonId) {
      queueMicrotask(() => {
        setChoices([]);
        setSelectedChoiceKeys({});
      });
      return;
    }
    let cancelled = false;
    const loadChoices = async (): Promise<void> => {
      setLoadingChoices(true);
      setError(null);
      try {
        const evidences = await Promise.all([
          loadGoldenProfileEvidence(fromPersonId),
          loadGoldenProfileEvidence(targetPersonId),
        ]);
        if (cancelled) return;
        const nextChoices = buildGoldenProfileChoices(targetPersonId, evidences);
        setChoices(nextChoices);
        setSelectedChoiceKeys(defaultChoiceByField(nextChoices));
      } catch (err: unknown) {
        if (cancelled) return;
        const message = err instanceof BffError ? err.message : "Could not load merge choices.";
        setError(message);
        setChoices([]);
        setSelectedChoiceKeys({});
      } finally {
        if (!cancelled) setLoadingChoices(false);
      }
    };
    void loadChoices();
    return () => {
      cancelled = true;
    };
  }, [fromPersonId, open, targetPersonId]);

  const handleSubmit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const body: ManualMergeRequestBody = {
        from_person_id: fromPersonId,
        to_person_id: targetPersonId,
        reason: reason.trim(),
        recompute_golden_profile: recompute,
        golden_profile_selections: selectedChoices(choices, selectedChoiceKeys).map(selectionBody),
      };
      const result: ManualMergeResponseBody = await bffFetch<ManualMergeResponseBody>(
        "/bff/persons/manual-merge",
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

  const canSubmit: boolean =
    targetPersonId.length > 0 &&
    targetPersonId !== fromPersonId &&
    reason.trim().length > 0 &&
    !submitting &&
    !loadingChoices;

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>{title}</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error !== null ? <Alert severity="error">{error}</Alert> : null}
          <TextField
            label="Target person id"
            value={toPersonId}
            onChange={(e) => setToPersonId(e.target.value)}
            fullWidth
            required
            disabled={lockTarget}
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
          {loadingChoices ? <CircularProgress size={24} /> : null}
          {choices.length > 0 ? (
            <GoldenProfilePicker
              choices={choices}
              selectedChoiceKeys={selectedChoiceKeys}
              onChange={(fieldName, choiceKey) =>
                setSelectedChoiceKeys((current) => ({ ...current, [fieldName]: choiceKey }))
              }
              disabled={submitting}
            />
          ) : null}
          <FormControlLabel
            control={
              <Checkbox checked={recompute} onChange={(e) => setRecompute(e.target.checked)} />
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


function selectedChoices(
  choices: readonly GoldenProfileChoice[],
  selectedChoiceKeys: Partial<Record<GoldenProfileFieldName, string>>,
): GoldenProfileChoice[] {
  const selectedKeys = new Set(Object.values(selectedChoiceKeys).filter((value) => value !== undefined));
  return choices.filter((choice) => selectedKeys.has(choice.key));
}
