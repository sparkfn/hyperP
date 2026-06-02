"use client";

import { useEffect, useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import GoldenProfilePicker from "@/components/GoldenProfilePicker";
import { useToast } from "@/components/ToastProvider";
import { BffError, bffFetch } from "@/lib/api-client";
import type {
  SurvivorshipOverrideBatchRequestBody,
  SurvivorshipOverrideBatchResponseBody,
} from "@/lib/api-types-person";
import {
  buildGoldenProfileChoices,
  goldenProfileFieldLabel,
  loadGoldenProfileEvidence,
  type GoldenProfileChoice,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";

function formatAddress(addr: { unit_number: string | null; street_number: string | null; street_name: string | null; city: string | null; postal_code: string | null } | null): string | undefined {
  if (!addr) return undefined;
  return [addr.unit_number, addr.street_number, addr.street_name, addr.city, addr.postal_code]
    .filter(Boolean)
    .join(" ") || undefined;
}

interface Props {
  open: boolean;
  personId: string;
  onClose: () => void;
  onSaved?: (response: SurvivorshipOverrideBatchResponseBody) => void;
}

export default function SurvivorshipOverrideDialog({
  open,
  personId,
  onClose,
  onSaved,
}: Props): ReactElement {
  const [loading, setLoading] = useState<boolean>(false);
  const [choices, setChoices] = useState<GoldenProfileChoice[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [initialKeys, setInitialKeys] = useState<Partial<Record<GoldenProfileFieldName, string>>>({});
  const [selectedKeys, setSelectedKeys] = useState<Partial<Record<GoldenProfileFieldName, string>>>({});
  const [readOnlyValues, setReadOnlyValues] = useState<Partial<Record<GoldenProfileFieldName, string>>>({});
  const [reason, setReason] = useState<string>("");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const router = useRouter();
  const { showToast } = useToast();

  useEffect(() => {
    if (!open) {
      queueMicrotask(() => {
        setLoading(false);
        setChoices([]);
        setLoadError(null);
        setInitialKeys({});
        setSelectedKeys({});
        setReadOnlyValues({});
        setReason("");
        setSubmitError(null);
      });
      return;
    }
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setLoading(true);
      setLoadError(null);
    });
    void loadGoldenProfileEvidence(personId)
      .then((evidence) => {
        if (cancelled) return;
        const all = buildGoldenProfileChoices(personId, [evidence]).filter(
          (c) => c.sourceKind === "source_record_fact",
        );
        all.sort((a, b) => (b.observedAt > a.observedAt ? 1 : b.observedAt < a.observedAt ? -1 : 0));
        setChoices(all);

        const currentValues: Partial<Record<GoldenProfileFieldName, string>> = {
          preferred_full_name: evidence.person.preferred_full_name ?? undefined,
          preferred_phone: evidence.person.preferred_phone ?? undefined,
          preferred_email: evidence.person.preferred_email ?? undefined,
          preferred_dob: evidence.person.preferred_dob ?? undefined,
        };

        const keys: Partial<Record<GoldenProfileFieldName, string>> = {};
        for (const field of Object.keys(currentValues) as GoldenProfileFieldName[]) {
          const fieldChoices = all.filter((c) => c.fieldName === field);
          if (fieldChoices.length === 0) continue;
          const currentVal = currentValues[field];
          const match = currentVal ? fieldChoices.find((c) => c.value === currentVal) : undefined;
          keys[field] = match ? match.key : fieldChoices[0]!.key;
        }
        setInitialKeys(keys);
        setSelectedKeys(keys);

        const roValues: Partial<Record<GoldenProfileFieldName, string>> = {};
        if (!all.some((c) => c.fieldName === "preferred_nric") && evidence.person.preferred_nric) {
          roValues["preferred_nric"] = evidence.person.preferred_nric;
        }
        if (!all.some((c) => c.fieldName === "preferred_address")) {
          const formatted = formatAddress(evidence.person.preferred_address);
          if (formatted) roValues["preferred_address"] = formatted;
        }
        setReadOnlyValues(roValues);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof BffError ? err.message : "Could not load source record facts.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, personId]);

  const handleSubmit = async (): Promise<void> => {
    const overrides = (Object.entries(selectedKeys) as [GoldenProfileFieldName, string][]).flatMap(
      ([field, key]) => {
        if (!key || key === initialKeys[field]) return [];
        const choice = choices.find((c) => c.key === key);
        if (choice?.sourceRecordPk == null) return [];
        return [{ attribute_name: field, selected_source_record_pk: choice.sourceRecordPk }];
      },
    );
    if (overrides.length === 0) return;

    setSubmitting(true);
    setSubmitError(null);
    try {
      const body: SurvivorshipOverrideBatchRequestBody = { overrides, reason: reason.trim() };
      const result = await bffFetch<SurvivorshipOverrideBatchResponseBody>(
        `/bff/persons/${encodeURIComponent(personId)}/survivorship-overrides/batch`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (onSaved) onSaved(result);
      const labels = result.applied
        .map((a) => goldenProfileFieldLabel(a as GoldenProfileFieldName))
        .join(", ");
      showToast(`Override saved for ${labels}`, "success");
      onClose();
      router.refresh();
    } catch (err: unknown) {
      const message = err instanceof BffError ? err.message : "Override failed.";
      setSubmitError(message);
      showToast(message, "error");
    } finally {
      setSubmitting(false);
    }
  };

  const hasSelection = (Object.keys(selectedKeys) as GoldenProfileFieldName[]).some(
    (f) => selectedKeys[f] !== initialKeys[f],
  );
  const canSubmit = hasSelection && reason.trim().length > 0 && !submitting && !loading;
  const hasChoices = choices.length > 0 || Object.keys(readOnlyValues).length > 0;

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle>Override golden profile fields</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {loadError !== null ? <Alert severity="error">{loadError}</Alert> : null}
          {submitError !== null ? <Alert severity="error">{submitError}</Alert> : null}
          {loading ? (
            <Box sx={{ display: "flex", justifyContent: "center", py: 2 }}>
              <CircularProgress size={24} />
            </Box>
          ) : !hasChoices ? (
            <Typography variant="body2" color="text.secondary">
              No source record facts found for this person.
            </Typography>
          ) : (
            <GoldenProfilePicker
              choices={choices}
              selectedChoiceKeys={selectedKeys}
              originalChoiceKeys={initialKeys}
              readOnlyValues={readOnlyValues}
              onChange={(field, key) =>
                setSelectedKeys((prev) => ({ ...prev, [field]: key }))
              }
              disabled={submitting}
              description="Select a different source record to override any field. Changed fields are highlighted."
            />
          )}
          <TextField
            label="Reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            multiline
            minRows={2}
            fullWidth
            required
            disabled={submitting || loading}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button onClick={handleSubmit} variant="contained" disabled={!canSubmit}>
          Save overrides
        </Button>
      </DialogActions>
    </Dialog>
  );
}
