import type { ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import ReviewQueueFilters from "@/components/ReviewQueueFilters";
import { apiFetch } from "@/lib/api-server";
import type { ReviewCaseSummary } from "@/lib/api-types-ops";

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

function pickString(
  params: Record<string, string | string[] | undefined>,
  key: string,
): string | undefined {
  const raw: string | string[] | undefined = params[key];
  if (typeof raw === "string" && raw.length > 0) return raw;
  return undefined;
}

async function loadReviewCases(
  query: Record<string, string>,
): Promise<{ items: ReviewCaseSummary[]; error: string | null }> {
  try {
    const res = await apiFetch<ReviewCaseSummary[]>("/review-cases", { query });
    return { items: res.data, error: null };
  } catch (err: unknown) {
    const message: string = err instanceof Error ? err.message : "Failed to load review queue.";
    return { items: [], error: message };
  }
}

function queueStateColor(
  state: string,
): "success" | "default" | "warning" | "info" | "error" {
  if (state === "open") return "info";
  if (state === "assigned") return "warning";
  if (state === "deferred") return "default";
  if (state === "resolved") return "success";
  if (state === "cancelled") return "error";
  return "default";
}

export default async function ReviewQueuePage({ searchParams }: PageProps): Promise<ReactElement> {
  const params: Record<string, string | string[] | undefined> = await searchParams;

  const query: Record<string, string> = {};
  const queueState: string | undefined = pickString(params, "queue_state");
  const assignedTo: string | undefined = pickString(params, "assigned_to");
  const priorityLte: string | undefined = pickString(params, "priority_lte");
  if (queueState !== undefined) query.queue_state = queueState;
  if (assignedTo !== undefined) query.assigned_to = assignedTo;
  if (priorityLte !== undefined) query.priority_lte = priorityLte;

  const { items, error } = await loadReviewCases(query);

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Review Queue
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Match decisions awaiting human adjudication.
        </Typography>
      </Box>

      <ReviewQueueFilters
        initialQueueState={queueState ?? ""}
        initialAssignedTo={assignedTo ?? ""}
        initialPriorityLte={priorityLte ?? ""}
      />

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      <Paper elevation={0} variant="outlined">
        {items.length === 0 ? (
          <Box sx={{ p: 3 }}>
            <Typography variant="body2" color="text.secondary">
              No review cases match the current filters.
            </Typography>
          </Box>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Case</TableCell>
                <TableCell>State</TableCell>
                <TableCell align="right">Priority</TableCell>
                <TableCell>Assigned to</TableCell>
                <TableCell>Engine</TableCell>
                <TableCell align="right">Confidence</TableCell>
                <TableCell>Decision</TableCell>
                <TableCell>SLA due</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((rc) => (
                <TableRow key={rc.review_case_id} hover>
                  <TableCell>
                    <Link
                      href={`/review/${rc.review_case_id}`}
                      style={{ textDecoration: "none" }}
                    >
                      {rc.review_case_id}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={rc.queue_state}
                      size="small"
                      color={queueStateColor(rc.queue_state)}
                    />
                  </TableCell>
                  <TableCell align="right">{rc.priority}</TableCell>
                  <TableCell>{rc.assigned_to ?? "—"}</TableCell>
                  <TableCell>{rc.match_decision.engine_type}</TableCell>
                  <TableCell align="right">
                    {(rc.match_decision.confidence * 100).toFixed(1)}%
                  </TableCell>
                  <TableCell>{rc.match_decision.decision}</TableCell>
                  <TableCell>{rc.sla_due_at ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Paper>
    </Stack>
  );
}
