"use client";

import { useState, type ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import FormLabel from "@mui/material/FormLabel";
import MenuItem from "@mui/material/MenuItem";
import Radio from "@mui/material/Radio";
import RadioGroup from "@mui/material/RadioGroup";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { IngestRunMode, IngestRunResponse } from "@/lib/api-types-ops";

type RunType = "manual" | "scheduled" | "backfill";

const RUN_TYPES: readonly RunType[] = ["manual", "scheduled", "backfill"] as const;

function isRunType(value: string): value is RunType {
  return (RUN_TYPES as readonly string[]).includes(value);
}

function normalizeDumpPath(value: string): string {
  return value.trim().replaceAll("\\", "/");
}

function validateDumpPath(value: string): string | null {
  const normalized = normalizeDumpPath(value);
  if (normalized.length === 0) return "Dump path is required.";
  if (normalized.startsWith("/") || /^[A-Za-z]:\//u.test(normalized)) {
    return "Dump path must be relative to ./dumps.";
  }
  if (normalized.split("/").includes("..")) {
    return "Dump path must not contain parent traversal.";
  }
  return null;
}

interface Props {
  sourceKey: string;
}

export default function StartIngestionRunDialog({ sourceKey }: Props): ReactElement {
  const [open, setOpen] = useState<boolean>(false);
  const [runType, setRunType] = useState<RunType>("manual");
  const [mode, setMode] = useState<IngestRunMode>("batch");
  const [dumpFiles, setDumpFiles] = useState<string[]>([]);
  const [dumpPath, setDumpPath] = useState<string>("");
  const [loadingDumps, setLoadingDumps] = useState<boolean>(false);
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
    setMode("batch");
    setDumpPath("");
    setDumpFiles([]);
    void loadDumpFiles();
  }

  function handleClose(): void {
    if (submitting) return;
    setOpen(false);
  }

  async function loadDumpFiles(): Promise<void> {
    setLoadingDumps(true);
    try {
      const files: string[] = await bffFetch<string[]>("/bff/dumps");
      setDumpFiles(files);
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to load dump files.";
      setError(message);
    } finally {
      setLoadingDumps(false);
    }
  }

  async function handleSubmit(): Promise<void> {
    setError(null);
    const normalizedDumpPath: string | null = mode === "dump" ? normalizeDumpPath(dumpPath) : null;
    if (mode === "dump") {
      const dumpPathError = validateDumpPath(dumpPath);
      if (dumpPathError !== null) {
        setError(dumpPathError);
        return;
      }
    }

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
        `/bff/ingest/${encodeURIComponent(sourceKey)}/runs`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            run_type: runType,
            mode,
            dump_path: normalizedDumpPath,
            metadata: parsedMetadata,
          }),
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
            <FormLabel>Ingestion source</FormLabel>
            <RadioGroup
              value={mode}
              onChange={(e) => setMode(e.target.value === "dump" ? "dump" : "batch")}
            >
              <FormControlLabel value="batch" control={<Radio />} label="Direct ingestion" />
              <FormControlLabel value="dump" control={<Radio />} label="Database dump file" />
            </RadioGroup>
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
            {mode === "dump" ? (
              <Stack spacing={1}>
                <TextField
                  select
                  label="Choose dump file"
                  value={dumpFiles.includes(dumpPath) ? dumpPath : ""}
                  onChange={(e) => setDumpPath(e.target.value)}
                  size="small"
                  disabled={loadingDumps}
                  helperText="Files are listed recursively from ./dumps."
                >
                  {dumpFiles.map((file) => (
                    <MenuItem key={file} value={file}>
                      {file}
                    </MenuItem>
                  ))}
                </TextField>
                {loadingDumps ? (
                  <Stack direction="row" spacing={1} alignItems="center">
                    <CircularProgress size={16} />
                    <Typography variant="caption" color="text.secondary">
                      Loading dump files...
                    </Typography>
                  </Stack>
                ) : null}
                <TextField
                  label="Manual dump path"
                  value={dumpPath}
                  onChange={(e) => setDumpPath(e.target.value)}
                  size="small"
                  helperText="Relative to ./dumps, for example fundbox/archive/dump.sql."
                />
              </Stack>
            ) : null}
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
                <Typography variant="body2">
                  Mode: <strong>{created.mode}</strong>
                  {created.dump_path !== null ? (
                    <>
                      {" "}· Dump: <strong>{created.dump_path}</strong>
                    </>
                  ) : null}
                </Typography>
                <Link href={`/ingestion/runs/${created.ingest_run_id}`}>View run details</Link>
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
