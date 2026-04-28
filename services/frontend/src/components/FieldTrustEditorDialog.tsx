"use client";

import { useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import {
  TRUST_TIERS,
  isTrustTier,
  type FieldTrustResponse,
  type TrustTier,
} from "@/lib/api-types-ops";

interface Props {
  sourceKey: string;
}

function coerceTiers(raw: Record<string, string>): Record<string, TrustTier> {
  const out: Record<string, TrustTier> = {};
  for (const [field, tier] of Object.entries(raw)) {
    if (isTrustTier(tier)) {
      out[field] = tier;
    } else {
      out[field] = "tier_4";
    }
  }
  return out;
}

export default function FieldTrustEditorDialog({ sourceKey }: Props): ReactElement {
  const router = useRouter();
  const [open, setOpen] = useState<boolean>(false);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [trust, setTrust] = useState<Record<string, TrustTier>>({});

  async function handleOpen(): Promise<void> {
    setOpen(true);
    setError(null);
    setLoading(true);
    try {
      const res: FieldTrustResponse = await bffFetch<FieldTrustResponse>(
        `/bff/source-systems/${encodeURIComponent(sourceKey)}/field-trust`,
      );
      setTrust(coerceTiers(res.field_trust));
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error ? err.message : "Failed to load field trust.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  function handleClose(): void {
    if (saving) return;
    setOpen(false);
  }

  function updateField(field: string, tier: TrustTier): void {
    setTrust((prev) => ({ ...prev, [field]: tier }));
  }

  async function handleSave(): Promise<void> {
    setSaving(true);
    setError(null);
    try {
      await bffFetch<FieldTrustResponse>(
        `/bff/source-systems/${encodeURIComponent(sourceKey)}/field-trust`,
        {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ updates: trust }),
        },
      );
      setOpen(false);
      router.refresh();
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error ? err.message : "Failed to save.";
      setError(message);
    } finally {
      setSaving(false);
    }
  }

  const entries: readonly [string, TrustTier][] = Object.entries(trust);

  return (
    <>
      <Button variant="outlined" size="small" onClick={handleOpen}>
        Edit field trust
      </Button>
      <Dialog open={open} onClose={handleClose} fullWidth maxWidth="sm">
        <DialogTitle>Field trust — {sourceKey}</DialogTitle>
        <DialogContent dividers>
          {loading ? (
            <Stack alignItems="center" sx={{ py: 4 }}>
              <CircularProgress size={28} />
            </Stack>
          ) : (
            <Stack spacing={2} sx={{ pt: 1 }}>
              {error !== null ? <Alert severity="error">{error}</Alert> : null}
              {entries.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No field trust configured for this source system.
                </Typography>
              ) : (
                entries.map(([field, tier]) => (
                  <TextField
                    key={field}
                    select
                    size="small"
                    label={field}
                    value={tier}
                    onChange={(e) => {
                      if (isTrustTier(e.target.value)) updateField(field, e.target.value);
                    }}
                  >
                    {TRUST_TIERS.map((t) => (
                      <MenuItem key={t} value={t}>
                        {t}
                      </MenuItem>
                    ))}
                  </TextField>
                ))
              )}
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            variant="contained"
            onClick={handleSave}
            disabled={loading || saving || entries.length === 0}
          >
            {saving ? "Saving..." : "Save"}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
