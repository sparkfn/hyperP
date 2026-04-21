"use client";

import { useCallback, useEffect, useState, type ReactElement } from "react";
import { useParams, useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { ReportDetail } from "@/lib/api-types";
import ReportExecutePanel from "@/components/ReportExecutePanel";
import ReportForm, { type ReportFormPayload } from "@/components/ReportForm";
import Gate from "@/components/auth/Gate";

type PageMode = "view" | "edit";

export default function ReportDetailPage(): ReactElement {
  const params = useParams<{ reportKey: string }>();
  const router = useRouter();
  const reportKey = params.reportKey;

  const [report, setReport] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [mode, setMode] = useState<PageMode>("view");
  const [saving, setSaving] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState<boolean>(false);
  const [deleting, setDeleting] = useState<boolean>(false);

  const loadReport = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await bffFetch<ReportDetail>(
        `/api/reports/${encodeURIComponent(reportKey)}`,
      );
      setReport(data);
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to load report.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [reportKey]);

  useEffect(() => {
    void loadReport();
  }, [loadReport]);

  function handleEditSave(payload: ReportFormPayload): void {
    void doEditSave(payload);
  }

  async function doEditSave(payload: ReportFormPayload): Promise<void> {
    setSaving(true);
    setSaveError(null);
    try {
      await bffFetch<ReportDetail>(
        `/api/reports/${encodeURIComponent(reportKey)}`,
        {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      setMode("view");
      await loadReport();
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to update report.";
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(): Promise<void> {
    setDeleting(true);
    try {
      await bffFetch<Record<string, string>>(
        `/api/reports/${encodeURIComponent(reportKey)}`,
        { method: "DELETE" },
      );
      router.push("/reports");
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to delete report.";
      setError(msg);
      setDeleteOpen(false);
    } finally {
      setDeleting(false);
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }
  if (error && !report) return <Alert severity="error">{error}</Alert>;
  if (!report) return <Alert severity="info">Report not found.</Alert>;

  if (mode === "edit") {
    return (
      <Stack spacing={3}>
        <Box>
          <Typography variant="h4" fontWeight={600}>
            Edit Report
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Modify the report definition, query, and parameters.
          </Typography>
        </Box>
        {saveError !== null ? <Alert severity="error">{saveError}</Alert> : null}
        <ReportForm
          mode="edit"
          initialData={report}
          saving={saving}
          onSave={handleEditSave}
          onCancel={() => {
            setMode("view");
            setSaveError(null);
          }}
        />
      </Stack>
    );
  }

  return (
    <Stack spacing={3}>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <Box>
          <Typography variant="h4" fontWeight={600}>
            {report.display_name}
          </Typography>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.5 }}>
            {report.description ? (
              <Typography variant="body2" color="text.secondary">
                {report.description}
              </Typography>
            ) : null}
            {report.category ? (
              <Chip label={report.category} size="small" variant="outlined" />
            ) : null}
          </Stack>
        </Box>
        <Gate mode="admin">
          <Stack direction="row" spacing={1}>
            <Button variant="outlined" size="small" onClick={() => setMode("edit")}>
              Edit
            </Button>
            <Button
              variant="outlined"
              size="small"
              color="error"
              onClick={() => setDeleteOpen(true)}
            >
              Delete
            </Button>
          </Stack>
        </Gate>
      </Box>

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
        <Typography variant="subtitle2" sx={{ mb: 2 }}>
          Cypher Query
        </Typography>
        <Box
          component="pre"
          sx={{
            p: 2,
            backgroundColor: "grey.100",
            borderRadius: 1,
            overflow: "auto",
            fontSize: "0.8rem",
            fontFamily: "monospace",
          }}
        >
          {report.cypher_query}
        </Box>
      </Paper>

      <ReportExecutePanel report={report} />

      <Dialog open={deleteOpen} onClose={() => setDeleteOpen(false)}>
        <DialogTitle>Delete Report</DialogTitle>
        <DialogContent>
          <DialogContentText>
            Are you sure you want to delete &quot;{report.display_name}&quot;? This action
            cannot be undone.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDeleteOpen(false)} disabled={deleting}>
            Cancel
          </Button>
          <Button
            color="error"
            onClick={() => void handleDelete()}
            disabled={deleting}
          >
            {deleting ? <CircularProgress size={18} /> : "Delete"}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
