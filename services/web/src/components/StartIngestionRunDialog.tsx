"use client";

import { useState, type ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { IngestRunResponse } from "@/lib/api-types-ops";

type RunType = "manual" | "scheduled" | "backfill";

const RUN_TYPES: readonly RunType[] = ["manual", "scheduled", "backfill"] as const;

function isRunType(value: string): value is RunType {
  return (RUN_TYPES as readonly string[]).includes(value);
}

interface Props {
  sourceKey: string;
}

export default function StartIngestionRunDialog({ sourceKey }: Props): ReactElement {
  const [open, setOpen] = useState<boolean>(false);
  const [runType, setRunType] = useState<RunType>("manual");
  const [metadataText, setMetadataText] = useState<string>("{}");
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<IngestRunResponse | null>(null);

  function handleOpen(): void {
    setOpen(true);
    setError(null);
    setCreated(null);
    setMetadataText("{}");
    setRunType("manual");
  }

  function handleClose(): void {
    if (submitting) return;
    setOpen(false);
  }

  async function handleSubmit(): Promise<void> {
    setError(null);
    let parsedMetadata: unknown;
    try {
      parsedMetadata = JSON.parse(metadataText);
    } catch {
      setError("Metadata must be valid JSON.");
      return;
    }
    if (
      typeof parsedMetadata !== "object" ||
      parsedMetadata === null ||
      Array.isArray(parsedMetadata)
    ) {
      setError("Metadata must be a JSON object.");
      return;
    }

    setSubmitting(true);
    try {
      const result: IngestRunResponse = await bffFetch<IngestRunResponse>(
        `/api/ingest/${encodeURIComponent(sourceKey)}/runs`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ run_type: runType, metadata: parsedMetadata }),
        },
      );
      setCreated(result);
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error ? err.message : "Failed to start run.";
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <Button variant="outlined" size="small" onClick={handleOpen}>
        Start ingestion run
      </Button>
      <Dialog open={open} onClose={handleClose} fullWidth maxWidth="sm">
        <DialogTitle>Start ingestion run — {sourceKey}</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <TextField
              select
              label="Run type"
              value={runType}
              onChange={(e) => {
                if (isRunType(e.target.value)) setRunType(e.target.value);
              }}
              size="small"
            >
              {RUN_TYPES.map((t) => (
                <MenuItem key={t} value={t}>
                  {t}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              label="Metadata (JSON)"
              value={metadataText}
              onChange={(e) => setMetadataText(e.target.value)}
              multiline
              minRows={4}
              size="small"
              spellCheck={false}
            />
            {error !== null ? <Alert severity="error">{error}</Alert> : null}
            {created !== null ? (
              <Alert severity="success">
                <Typography variant="body2">
                  Started run <strong>{created.ingest_run_id}</strong>
                </Typography>
                <Link href={`/ingestion/runs/${created.ingest_run_id}`}>
                  View run details
                </Link>
              </Alert>
            ) : null}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={handleClose} disabled={submitting}>
            Close
          </Button>
          <Button
            variant="contained"
            onClick={handleSubmit}
            disabled={submitting || created !== null}
          >
            {submitting ? "Starting..." : "Start run"}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
