import type { ReactElement } from "react";
import Link from "next/link";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import ReviewQueueFilters, { type ReviewFilterValues } from "@/components/ReviewQueueFilters";
import { apiFetch } from "@/lib/api-server";
import type { ApiResponse } from "@/lib/api-types";
import type { ReviewCaseSummary } from "@/lib/api-types-ops";

interface PageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

// Server-side params forwarded verbatim to the API list endpoint.
const FILTER_KEYS: readonly string[] = [
  "queue_state", "resolved", "assigned_to", "person_id", "q", "decision", "engine_type",
  "priority_gte", "priority_lte", "confidence_gte", "confidence_lte",
  "created_after", "created_before", "sla_due_after", "sla_due_before",
  "overdue_sla", "sort_by", "sort_order",
];

const DEFAULT_LIMIT = 25;

function pickString(
  params: Record<string, string | string[] | undefined>,
  key: string,
): string {
  const raw: string | string[] | undefined = params[key];
  return typeof raw === "string" ? raw : "";
}

// Cursor is base64 of the integer skip offset (mirrors the API's encode_cursor).
function encodeCursor(offset: number): string {
  return Buffer.from(String(offset), "ascii").toString("base64");
}
function decodeCursor(cursor: string): number {
  if (!cursor) return 0;
  try {
    const n = Number.parseInt(Buffer.from(cursor, "base64").toString("ascii"), 10);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  } catch {
    return 0;
  }
}

async function loadReviewCases(
  query: Record<string, string>,
): Promise<{ items: ReviewCaseSummary[]; total: number | null; error: string | null }> {
  try {
    const res: ApiResponse<ReviewCaseSummary[]> = await apiFetch<ReviewCaseSummary[]>("/review-cases", { query });
    return { items: res.data, total: res.meta.total_count ?? null, error: null };
  } catch (err: unknown) {
    const message: string = err instanceof Error ? err.message : "Failed to load review queue.";
    return { items: [], total: null, error: message };
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

function buildPageHref(params: Record<string, string>, cursor: string | null): string {
  const next = new URLSearchParams(params);
  if (cursor) next.set("cursor", cursor);
  else next.delete("cursor");
  const qs = next.toString();
  return qs.length > 0 ? `/review?${qs}` : "/review";
}

export default async function ReviewQueuePage({ searchParams }: PageProps): Promise<ReactElement> {
  const params: Record<string, string | string[] | undefined> = await searchParams;

  // Forward all active filter params to the API.
  const query: Record<string, string> = {};
  for (const key of FILTER_KEYS) {
    const value: string = pickString(params, key);
    if (value.length > 0) query[key] = value;
  }
  const limitRaw: number = Number.parseInt(pickString(params, "limit"), 10);
  const limit: number = Number.isFinite(limitRaw) && limitRaw > 0 ? limitRaw : DEFAULT_LIMIT;
  query.limit = String(limit);

  const cursorParam: string = pickString(params, "cursor");
  const skip: number = decodeCursor(cursorParam);
  if (cursorParam) query.cursor = cursorParam;

  const { items, total, error } = await loadReviewCases(query);

  // Build filter form initial values (everything except cursor).
  const filterInitial: ReviewFilterValues = {
    queue_state: pickString(params, "queue_state"),
    resolved: pickString(params, "resolved"),
    assigned_to: pickString(params, "assigned_to"),
    person_id: pickString(params, "person_id"),
    q: pickString(params, "q"),
    decision: pickString(params, "decision"),
    engine_type: pickString(params, "engine_type"),
    priority_gte: pickString(params, "priority_gte"),
    priority_lte: pickString(params, "priority_lte"),
    confidence_gte: pickString(params, "confidence_gte"),
    confidence_lte: pickString(params, "confidence_lte"),
    created_after: pickString(params, "created_after"),
    created_before: pickString(params, "created_before"),
    sla_due_after: pickString(params, "sla_due_after"),
    sla_due_before: pickString(params, "sla_due_before"),
    overdue_sla: pickString(params, "overdue_sla") === "true",
    sort_by: pickString(params, "sort_by"),
    sort_order: pickString(params, "sort_order"),
    limit: pickString(params, "limit"),
  };

  // Pagination math (forward + back via base64 skip offsets).
  const urlParams: Record<string, string> = { ...query };
  delete urlParams.cursor;
  const hasPrev: boolean = skip > 0;
  const hasNext: boolean = total !== null ? skip + limit < total : items.length === limit;
  const prevHref: string = buildPageHref(urlParams, skip - limit > 0 ? encodeCursor(skip - limit) : null);
  const nextHref: string = buildPageHref(urlParams, encodeCursor(skip + limit));
  const showFrom: number = items.length === 0 ? 0 : skip + 1;
  const showTo: number = skip + items.length;

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

      <ReviewQueueFilters initial={filterInitial} />

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      <Paper elevation={0} variant="outlined">
        <Box sx={{ display: "flex", alignItems: "center", px: 2, py: 1 }}>
          <Typography variant="body2" color="text.secondary">
            {items.length === 0
              ? "No review cases match the current filters."
              : `Showing ${showFrom}–${showTo}${total !== null ? ` of ${total}` : ""}`}
          </Typography>
          <Stack direction="row" spacing={1} sx={{ ml: "auto" }}>
            {hasPrev ? (
              <Button size="small" component={Link} href={prevHref}>← Prev</Button>
            ) : (
              <Button size="small" disabled>← Prev</Button>
            )}
            {hasNext ? (
              <Button size="small" component={Link} href={nextHref}>Next →</Button>
            ) : (
              <Button size="small" disabled>Next →</Button>
            )}
          </Stack>
        </Box>

        {items.length > 0 && (
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
