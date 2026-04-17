"use client";

import { useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { ReportDetail } from "@/lib/api-types";
import ReportForm, { type ReportFormPayload } from "@/components/ReportForm";

export default function NewReportPage(): ReactElement {
  const router = useRouter();
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  function handleSave(payload: ReportFormPayload): void {
    void doSave(payload);
  }

  async function doSave(payload: ReportFormPayload): Promise<void> {
    setSaving(true);
    setError(null);
    try {
      await bffFetch<ReportDetail>("/api/reports", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      router.push(`/reports/${encodeURIComponent(payload.report_key)}`);
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to create report.";
      setError(msg);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Create Report
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Define a new custom Cypher report with optional parameters.
        </Typography>
      </Box>

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      <ReportForm
        mode="create"
        saving={saving}
        onSave={handleSave}
        onCancel={() => router.push("/reports")}
      />
    </Stack>
  );
}
