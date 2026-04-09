import type { ReactElement } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import ReviewActionsPanel from "@/components/ReviewActionsPanel";
import { UpstreamError, apiFetch } from "@/lib/api-server";
import type {
  PersonComparisonEntity,
  ReviewCaseDetail,
} from "@/lib/api-types-ops";

interface PageProps {
  params: Promise<{ reviewCaseId: string }>;
}

async function loadReviewCase(reviewCaseId: string): Promise<ReviewCaseDetail> {
  try {
    const res = await apiFetch<ReviewCaseDetail>(
      `/review-cases/${encodeURIComponent(reviewCaseId)}`,
    );
    return res.data;
  } catch (err: unknown) {
    if (err instanceof UpstreamError && err.status === 404) {
      notFound();
    }
    throw err;
  }
}

export default async function ReviewCaseDetailPage({ params }: PageProps): Promise<ReactElement> {
  const { reviewCaseId } = await params;
  const detail: ReviewCaseDetail = await loadReviewCase(reviewCaseId);

  return (
    <Stack spacing={3}>
      <Box>
        <Button component={Link} href="/review" size="small">
          ← Back to review queue
        </Button>
      </Box>

      <CaseHeader detail={detail} />

      <MatchDecisionCard detail={detail} />

      <ComparisonCard
        left={detail.comparison_left}
        right={detail.comparison_right}
      />

      <ReviewActionsPanel
        reviewCaseId={detail.review_case_id}
        queueState={detail.queue_state}
        assignedTo={detail.assigned_to}
      />

      <ActionHistoryCard actions={detail.actions} />
    </Stack>
  );
}

function CaseHeader({ detail }: { detail: ReviewCaseDetail }): ReactElement {
  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h5" fontWeight={600}>
          Review Case
        </Typography>
        <Chip label={detail.queue_state} size="small" />
        {detail.resolution !== null ? (
          <Chip label={`resolution: ${detail.resolution}`} size="small" color="success" />
        ) : null}
      </Stack>
      <Typography variant="caption" color="text.secondary" display="block">
        {detail.review_case_id}
      </Typography>
      <Divider sx={{ my: 2 }} />
      <Grid container spacing={2}>
        <Field label="Priority" value={String(detail.priority)} />
        <Field label="Assigned to" value={detail.assigned_to} />
        <Field label="SLA due" value={detail.sla_due_at} />
        <Field label="Follow-up" value={detail.follow_up_at} />
        <Field label="Created" value={detail.created_at} />
        <Field label="Updated" value={detail.updated_at} />
      </Grid>
    </Paper>
  );
}

function MatchDecisionCard({ detail }: { detail: ReviewCaseDetail }): ReactElement {
  const md = detail.match_decision;
  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Match Decision
      </Typography>
      <Grid container spacing={2}>
        <Field label="Decision" value={md.decision} />
        <Field label="Confidence" value={`${(md.confidence * 100).toFixed(1)}%`} />
        <Field label="Engine" value={`${md.engine_type} ${md.engine_version}`} />
        <Field label="Policy version" value={md.policy_version} />
        <Field label="Left person" value={md.left_person_id} />
        <Field label="Right person" value={md.right_person_id} />
      </Grid>
      <Divider sx={{ my: 2 }} />
      <Typography variant="subtitle2">Reasons</Typography>
      {md.reasons.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          —
        </Typography>
      ) : (
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          {md.reasons.map((r) => (
            <Chip key={r} label={r} size="small" />
          ))}
        </Stack>
      )}
      {md.blocking_conflicts.length > 0 ? (
        <>
          <Typography variant="subtitle2" sx={{ mt: 2 }}>
            Blocking conflicts
          </Typography>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            {md.blocking_conflicts.map((c) => (
              <Chip key={c} label={c} size="small" color="error" />
            ))}
          </Stack>
        </>
      ) : null}
    </Paper>
  );
}

interface ComparisonProps {
  left: PersonComparisonEntity | null;
  right: PersonComparisonEntity | null;
}

function ComparisonCard({ left, right }: ComparisonProps): ReactElement {
  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Person Comparison
      </Typography>
      <Grid container spacing={2}>
        <Grid size={{ xs: 12, md: 6 }}>
          <ComparisonColumn title="Left" entity={left} />
        </Grid>
        <Grid size={{ xs: 12, md: 6 }}>
          <ComparisonColumn title="Right" entity={right} />
        </Grid>
      </Grid>
    </Paper>
  );
}

function ComparisonColumn({
  title,
  entity,
}: {
  title: string;
  entity: PersonComparisonEntity | null;
}): ReactElement {
  if (entity === null) {
    return (
      <Paper variant="outlined" sx={{ p: 2 }}>
        <Typography variant="subtitle2">{title}</Typography>
        <Typography variant="body2" color="text.secondary">
          (not available)
        </Typography>
      </Paper>
    );
  }
  const kindLabel: string =
    entity.entity_kind === "source_record" ? "Inbound source record" : "Existing person";
  const kindColor: "info" | "default" = entity.entity_kind === "source_record" ? "info" : "default";
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle2">{title}</Typography>
        <Chip label={kindLabel} size="small" color={kindColor} variant="outlined" />
        {entity.status !== null ? <Chip label={entity.status} size="small" /> : null}
      </Stack>
      {entity.person_id !== null ? (
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
          <Link
            href={`/persons/${entity.person_id}`}
            style={{ color: "inherit", textDecoration: "underline" }}
          >
            {entity.person_id}
          </Link>
        </Typography>
      ) : null}
      {entity.source_record_id !== null ? (
        <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: 1 }}>
          source_record_id: {entity.source_record_id}
        </Typography>
      ) : null}
      <Grid container spacing={1}>
        <Field label="Name" value={entity.preferred_full_name} />
        <Field label="Phone" value={entity.preferred_phone} />
        <Field label="Email" value={entity.preferred_email} />
        <Field label="DOB" value={entity.preferred_dob} />
        <Field
          label="Address"
          value={entity.preferred_address?.normalized_full ?? null}
          full
        />
      </Grid>
    </Paper>
  );
}

function ActionHistoryCard({
  actions,
}: {
  actions: ReviewCaseDetail["actions"];
}): ReactElement {
  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Action History
      </Typography>
      {actions.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No actions recorded.
        </Typography>
      ) : (
        <Stack spacing={1}>
          {actions.map((a, idx) => (
            <Paper key={idx} variant="outlined" sx={{ p: 1.5 }}>
              <Typography variant="body2">
                <strong>{a.action_type ?? "action"}</strong>
                {a.actor_id !== null && a.actor_id !== undefined ? ` — ${a.actor_id}` : ""}
                {a.created_at !== null && a.created_at !== undefined ? ` @ ${a.created_at}` : ""}
              </Typography>
              {a.notes !== null && a.notes !== undefined && a.notes !== "" ? (
                <Typography variant="caption" color="text.secondary">
                  {a.notes}
                </Typography>
              ) : null}
            </Paper>
          ))}
        </Stack>
      )}
    </Paper>
  );
}

interface FieldProps {
  label: string;
  value: string | null;
  full?: boolean;
}

function Field({ label, value, full = false }: FieldProps): ReactElement {
  return (
    <Grid size={{ xs: 12, sm: full ? 12 : 6, md: full ? 12 : 4 }}>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="body2">{value ?? "—"}</Typography>
    </Grid>
  );
}
