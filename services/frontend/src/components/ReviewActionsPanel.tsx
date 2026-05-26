"use client";

import { useEffect, useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import GoldenProfilePicker from "@/components/GoldenProfilePicker";
import { useToast } from "@/components/ToastProvider";
import { BffError, bffFetch } from "@/lib/api-client";
import type { Person } from "@/lib/api-types";
import {
  REVIEW_ACTION_TYPES,
  type AssignReviewRequestBody,
  type ReviewActionRequestBody,
  type ReviewActionResponse,
  type ReviewActionType,
  type ReviewAssignResponse,
} from "@/lib/api-types-ops";
import type { PersonIdentifier, PersonSourceRecord } from "@/lib/api-types-person";
import {
  appendPageLimit,
  buildGoldenProfileChoices,
  defaultChoiceByField,
  selectionBody,
  type GoldenProfileChoice,
  type GoldenProfileEvidence,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";

interface Props {
  reviewCaseId: string;
  queueState: string;
  assignedTo: string | null;
  leftPersonId: string | null;
  rightPersonId: string | null;
}

function isReviewActionType(value: string): value is ReviewActionType {
  return (REVIEW_ACTION_TYPES as readonly string[]).includes(value);
}

export default function ReviewActionsPanel({
  reviewCaseId,
  queueState,
  assignedTo,
  leftPersonId,
  rightPersonId,
}: Props): ReactElement {
  const router = useRouter();
  const { showToast } = useToast();

  const [assignee, setAssignee] = useState<string>(assignedTo ?? "");
  const [assignBusy, setAssignBusy] = useState<boolean>(false);

  const [actionType, setActionType] = useState<ReviewActionType>("merge");
  const [notes, setNotes] = useState<string>("");
  const [followUpAt, setFollowUpAt] = useState<string>("");
  const [survivorPersonId, setSurvivorPersonId] = useState<string>(leftPersonId ?? "");
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [loadingChoices, setLoadingChoices] = useState<boolean>(false);
  const [choices, setChoices] = useState<GoldenProfileChoice[]>([]);
  const [selectedChoiceKeys, setSelectedChoiceKeys] = useState<
    Partial<Record<GoldenProfileFieldName, string>>
  >({});

  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const resolved: boolean = queueState === "resolved" || queueState === "cancelled";
  const canLoadMergeChoices = leftPersonId !== null && rightPersonId !== null;

  useEffect(() => {
    if (actionType !== "merge" || !canLoadMergeChoices || survivorPersonId.length === 0) {
      queueMicrotask(() => {
        setChoices([]);
        setSelectedChoiceKeys({});
      });
      return;
    }
    let cancelled = false;
    const loadChoices = async (): Promise<void> => {
      setLoadingChoices(true);
      setError(null);
      try {
        const evidences = await Promise.all([
          loadGoldenProfileEvidence(leftPersonId),
          loadGoldenProfileEvidence(rightPersonId),
        ]);
        if (cancelled) return;
        const nextChoices = buildGoldenProfileChoices(survivorPersonId, evidences);
        setChoices(nextChoices);
        setSelectedChoiceKeys(defaultChoiceByField(nextChoices));
      } catch (err: unknown) {
        if (cancelled) return;
        const message = err instanceof BffError ? err.message : "Could not load merge choices.";
        setError(message);
        setChoices([]);
        setSelectedChoiceKeys({});
      } finally {
        if (!cancelled) setLoadingChoices(false);
      }
    };
    void loadChoices();
    return () => {
      cancelled = true;
    };
  }, [actionType, canLoadMergeChoices, leftPersonId, rightPersonId, survivorPersonId]);

  async function onAssign(): Promise<void> {
    setError(null);
    setSuccess(null);
    if (assignee.trim().length === 0) {
      setError("Assignee is required.");
      return;
    }
    setAssignBusy(true);
    try {
      const body: AssignReviewRequestBody = { assigned_to: assignee.trim() };
      const result: ReviewAssignResponse = await bffFetch<ReviewAssignResponse>(
        `/bff/review-cases/${encodeURIComponent(reviewCaseId)}/assign`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      setSuccess(`Assigned to ${result.assigned_to}.`);
      showToast(`Assigned to ${result.assigned_to}`, "success");
      router.refresh();
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error ? err.message : "Assign failed.";
      setError(message);
      showToast(message, "error");
    } finally {
      setAssignBusy(false);
    }
  }

  async function onSubmitAction(): Promise<void> {
    setError(null);
    setSuccess(null);
    setActionBusy(true);
    try {
      const body: ReviewActionRequestBody = {
        action_type: actionType,
        notes: notes.trim().length > 0 ? notes.trim() : null,
        metadata: {
          follow_up_at: followUpAt.length > 0 ? followUpAt : null,
          survivor_person_id: actionType === "merge" ? survivorPersonId : null,
          golden_profile_selections:
            actionType === "merge"
              ? selectedChoices(choices, selectedChoiceKeys).map(selectionBody)
              : [],
        },
      };
      const result: ReviewActionResponse = await bffFetch<ReviewActionResponse>(
        `/bff/review-cases/${encodeURIComponent(reviewCaseId)}/actions`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const summary: string = `Action submitted. New state: ${result.queue_state}${
        result.resolution !== null ? ` (${result.resolution})` : ""
      }.`;
      setSuccess(summary);
      showToast(summary, "success");
      setNotes("");
      setFollowUpAt("");
      router.refresh();
    } catch (err: unknown) {
      const message: string =
        err instanceof BffError || err instanceof Error ? err.message : "Action failed.";
      setError(message);
      showToast(message, "error");
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Reviewer Actions
      </Typography>

      {error !== null ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      ) : null}
      {success !== null ? (
        <Alert severity="success" sx={{ mb: 2 }}>
          {success}
        </Alert>
      ) : null}

      {resolved ? (
        <Alert severity="info">This review case is {queueState} and no longer accepts actions.</Alert>
      ) : null}

      <Stack spacing={3}>
        <Box>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            Assign
          </Typography>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems="flex-end">
            <TextField
              size="small"
              label="Assignee"
              value={assignee}
              onChange={(e) => setAssignee(e.target.value)}
              placeholder="reviewer id"
              disabled={resolved}
              sx={{ minWidth: 240 }}
            />
            <Button variant="outlined" onClick={onAssign} disabled={assignBusy || resolved}>
              {assignBusy ? "Assigning…" : "Assign"}
            </Button>
          </Stack>
        </Box>

        <Box>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            Submit action
          </Typography>
          <Stack spacing={2}>
            <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
              <TextField
                select
                size="small"
                label="Action type"
                value={actionType}
                onChange={(e) => {
                  const v: string = e.target.value;
                  if (isReviewActionType(v)) setActionType(v);
                }}
                disabled={resolved}
                sx={{ minWidth: 220 }}
              >
                {REVIEW_ACTION_TYPES.map((t) => (
                  <MenuItem key={t} value={t}>
                    {t}
                  </MenuItem>
                ))}
              </TextField>
              <TextField
                size="small"
                label="Follow-up at (ISO)"
                value={followUpAt}
                onChange={(e) => setFollowUpAt(e.target.value)}
                placeholder="2026-04-15T12:00:00Z"
                disabled={resolved || actionType !== "defer"}
                sx={{ minWidth: 260 }}
              />
            </Stack>
            {actionType === "merge" && canLoadMergeChoices ? (
              <TextField
                select
                size="small"
                label="Survivor"
                value={survivorPersonId}
                onChange={(e) => setSurvivorPersonId(e.target.value)}
                disabled={resolved || actionBusy}
                sx={{ minWidth: 260 }}
              >
                <MenuItem value={leftPersonId}>Left person ({leftPersonId})</MenuItem>
                <MenuItem value={rightPersonId}>Right person ({rightPersonId})</MenuItem>
              </TextField>
            ) : null}
            {loadingChoices ? <CircularProgress size={24} /> : null}
            {actionType === "merge" && choices.length > 0 ? (
              <GoldenProfilePicker
                choices={choices}
                selectedChoiceKeys={selectedChoiceKeys}
                onChange={(fieldName, choiceKey) =>
                  setSelectedChoiceKeys((current) => ({ ...current, [fieldName]: choiceKey }))
                }
                disabled={resolved || actionBusy}
              />
            ) : null}
            <TextField
              label="Notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              multiline
              minRows={2}
              fullWidth
              disabled={resolved}
            />
            <Box>
              <Button
                variant="contained"
                onClick={onSubmitAction}
                disabled={actionBusy || resolved || loadingChoices}
              >
                {actionBusy ? "Submitting…" : "Submit action"}
              </Button>
            </Box>
          </Stack>
        </Box>
      </Stack>
    </Paper>
  );
}

async function loadGoldenProfileEvidence(personId: string): Promise<GoldenProfileEvidence> {
  const [person, sourceRecords, identifiers] = await Promise.all([
    bffFetch<Person>(`/bff/persons/${encodeURIComponent(personId)}`),
    bffFetch<PersonSourceRecord[]>(
      appendPageLimit(`/bff/persons/${encodeURIComponent(personId)}/source-records`),
    ),
    bffFetch<PersonIdentifier[]>(
      appendPageLimit(`/bff/persons/${encodeURIComponent(personId)}/identifiers`),
    ),
  ]);
  return { person, sourceRecords, identifiers };
}

function selectedChoices(
  choices: readonly GoldenProfileChoice[],
  selectedChoiceKeys: Partial<Record<GoldenProfileFieldName, string>>,
): GoldenProfileChoice[] {
  const selectedKeys = new Set(Object.values(selectedChoiceKeys).filter((value) => value !== undefined));
  return choices.filter((choice) => selectedKeys.has(choice.key));
}
