import type { ReactElement } from "react";
import { notFound } from "next/navigation";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Grid2 from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { UpstreamError, apiFetch } from "@/lib/api-server";
import type { IngestRunDetailResponse } from "@/lib/api-types-ops";

interface PageProps {
  params: Promise<{ runId: string }>;
}

function statusColor(
  status: string,
): "default" | "primary" | "success" | "warning" | "error" {
  if (status === "succeeded" || status === "completed") return "success";
  if (status === "running" || status === "started") return "primary";
  if (status === "failed" || status === "error") return "error";
  if (status === "pending" || status === "queued") return "warning";
  return "default";
}

async function loadRun(runId: string): Promise<IngestRunDetailResponse> {
  try {
    const res = await apiFetch<IngestRunDetailResponse>(
      `/ingest/runs/${encodeURIComponent(runId)}`,
    );
    return res.data;
  } catch (err: unknown) {
    if (err instanceof UpstreamError && err.status === 404) {
      notFound();
    }
    throw err;
  }
}

export default async function IngestRunDetailPage({ params }: PageProps): Promise<ReactElement> {
  const { runId } = await params;
  const run: IngestRunDetailResponse = await loadRun(runId);

  const metadata: Record<string, unknown> = {
    run_type: run.run_type,
    source_key: run.source_key,
    record_count: run.record_count,
    rejected_count: run.rejected_count,
    started_at: run.started_at,
    finished_at: run.finished_at,
  };

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Ingestion run
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {run.ingest_run_id}
        </Typography>
      </Box>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Stack spacing={2}>
          <Stack direction="row" spacing={2} alignItems="center">
            <Chip label={run.status} color={statusColor(run.status)} />
            <Chip label={run.run_type} variant="outlined" />
            {run.source_key !== null ? (
              <Chip label={run.source_key} variant="outlined" />
            ) : null}
          </Stack>

          <Grid2 container spacing={2}>
            <Grid2 size={{ xs: 12, sm: 6 }}>
              <Typography variant="caption" color="text.secondary">
                Started at
              </Typography>
              <Typography variant="body2">{run.started_at ?? "—"}</Typography>
            </Grid2>
            <Grid2 size={{ xs: 12, sm: 6 }}>
              <Typography variant="caption" color="text.secondary">
                Finished at
              </Typography>
              <Typography variant="body2">{run.finished_at ?? "—"}</Typography>
            </Grid2>
            <Grid2 size={{ xs: 6, sm: 3 }}>
              <Typography variant="caption" color="text.secondary">
                Records accepted
              </Typography>
              <Typography variant="body1">{run.record_count}</Typography>
            </Grid2>
            <Grid2 size={{ xs: 6, sm: 3 }}>
              <Typography variant="caption" color="text.secondary">
                Records rejected
              </Typography>
              <Typography variant="body1">{run.rejected_count}</Typography>
            </Grid2>
          </Grid2>
        </Stack>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle2" gutterBottom>
          Metadata
        </Typography>
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 2,
            backgroundColor: "#f5f5f5",
            fontSize: 12,
            overflowX: "auto",
            whiteSpace: "pre-wrap",
          }}
        >
          {JSON.stringify(metadata, null, 2)}
        </Box>
      </Paper>
    </Stack>
  );
}
