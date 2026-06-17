"use client";

import { useEffect, useState, type ReactElement } from "react";

import GoldenProfilePicker from "@/components/GoldenProfilePicker";
import { BffError, bffFetch } from "@/lib/api-client";
import {
  REVIEW_ACTION_TYPES,
  type AssignReviewRequestBody,
  type ReviewActionRequestBody,
  type ReviewActionResponse,
  type ReviewActionType,
  type ReviewAssignResponse,
} from "@/lib/api-types-ops";
import {
  buildGoldenProfileChoices,
  loadGoldenProfileEvidence,
  selectionBody,
  type GoldenProfileChoice,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";
import styles from "@/app/review/review.module.css";

interface ReviewActionsPanelProps {
  reviewCaseId: string;
  queueState: string;
  assignedTo: string | null;
  leftPersonId: string | null;
  rightPersonId: string | null;
  leftPersonStatus?: string | null;
  rightPersonStatus?: string | null;
  onChanged: () => void;
  embedded?: boolean;
}

function isReviewActionType(value: string): value is ReviewActionType {
  return (REVIEW_ACTION_TYPES as readonly string[]).includes(value);
}

function defaultRightChoiceByField(
  choices: readonly GoldenProfileChoice[],
  rightPersonId: string,
): Partial<Record<GoldenProfileFieldName, string>> {
  const defaults: Partial<Record<GoldenProfileFieldName, string>> = {};
  for (const choice of choices) {
    if (choice.personId === rightPersonId && defaults[choice.fieldName] === undefined) {
      defaults[choice.fieldName] = choice.key;
    }
  }
  for (const choice of choices) {
    if (defaults[choice.fieldName] === undefined) {
      defaults[choice.fieldName] = choice.key;
    }
  }
  return defaults;
}

export default function ReviewActionsPanel({
  reviewCaseId,
  queueState,
  assignedTo,
  leftPersonId,
  rightPersonId,
  leftPersonStatus,
  rightPersonStatus,
  onChanged,
  embedded = false,
}: ReviewActionsPanelProps): ReactElement {
  const [assignee, setAssignee] = useState<string>(assignedTo ?? "");
  const [assignBusy, setAssignBusy] = useState<boolean>(false);
  const [actionType, setActionType] = useState<ReviewActionType>("merge");
  const [notes, setNotes] = useState<string>("");
  const [followUpAt, setFollowUpAt] = useState<string>("");
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [loadingChoices, setLoadingChoices] = useState<boolean>(false);
  const [choices, setChoices] = useState<GoldenProfileChoice[]>([]);
  const [selectedChoiceKeys, setSelectedChoiceKeys] = useState<
    Partial<Record<GoldenProfileFieldName, string>>
  >({});
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const resolved = queueState === "resolved" || queueState === "cancelled";
  const canLoadMergeChoices = leftPersonId !== null && rightPersonId !== null;
  const mergeRequiresUnmerge = actionType === "merge" && (
    (leftPersonStatus !== undefined && leftPersonStatus !== null && leftPersonStatus !== "active") ||
    (rightPersonStatus !== undefined && rightPersonStatus !== null && rightPersonStatus !== "active")
  );
  const mergeSurvivorPersonId = rightPersonId ?? leftPersonId ?? "";

  useEffect(() => {
    if (actionType !== "merge" || !canLoadMergeChoices || mergeRequiresUnmerge || mergeSurvivorPersonId.length === 0) {
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
        const nextChoices = buildGoldenProfileChoices(mergeSurvivorPersonId, evidences);
        setChoices(nextChoices);
        setSelectedChoiceKeys(defaultRightChoiceByField(nextChoices, rightPersonId));
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof BffError ? err.message : "Could not load merge choices.");
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
  }, [actionType, canLoadMergeChoices, leftPersonId, mergeSurvivorPersonId, mergeRequiresUnmerge, rightPersonId]);

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
      const result = await bffFetch<ReviewAssignResponse>(
        `/bff/review-cases/${encodeURIComponent(reviewCaseId)}/assign`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      setSuccess(`Assigned to ${result.assigned_to}.`);
      onChanged();
    } catch (err: unknown) {
      setError(err instanceof BffError || err instanceof Error ? err.message : "Assign failed.");
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
          survivor_person_id: actionType === "merge" ? mergeSurvivorPersonId : null,
          golden_profile_selections:
            actionType === "merge"
              ? selectedChoices(choices, selectedChoiceKeys).map(selectionBody)
              : [],
        },
      };
      const result = await bffFetch<ReviewActionResponse>(
        `/bff/review-cases/${encodeURIComponent(reviewCaseId)}/actions`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      setSuccess(
        `Action submitted. New state: ${result.queue_state}${result.resolution !== null ? ` (${result.resolution})` : ""}.`,
      );
      setNotes("");
      setFollowUpAt("");
      onChanged();
    } catch (err: unknown) {
      setError(err instanceof BffError || err instanceof Error ? err.message : "Action failed.");
    } finally {
      setActionBusy(false);
    }
  }

  return (
    <section className={`${styles.detailCard} ${embedded ? styles.embeddedActionPanel : ""}`}>
      <div className={styles.cardHeader}>Reviewer Actions</div>
      {error !== null ? <div className={styles.errorBanner}>{error}</div> : null}
      {success !== null ? <div className={styles.successBanner}>{success}</div> : null}
      {resolved ? <div className={styles.infoBanner}>This review case is {queueState} and no longer accepts actions.</div> : null}

      <div className={styles.actionSection}>
        <div className={styles.sectionTitle}>Assign</div>
        <div className={styles.formRow}>
          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>Assignee</span>
            <input
              className={styles.input}
              value={assignee}
              onChange={(event) => setAssignee(event.target.value)}
              placeholder="reviewer id"
              disabled={resolved}
            />
          </label>
          <button className={styles.primaryBtn} onClick={() => void onAssign()} disabled={assignBusy || resolved}>
            {assignBusy ? "Assigning…" : "Assign"}
          </button>
        </div>
      </div>

      <div className={styles.actionSection}>
        <div className={styles.sectionTitle}>Submit action</div>
        <div className={styles.formRow}>
          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>Action type</span>
            <select
              className={styles.select}
              value={actionType}
              onChange={(event) => {
                if (isReviewActionType(event.target.value)) setActionType(event.target.value);
              }}
              disabled={resolved}
            >
              {REVIEW_ACTION_TYPES.map((type) => (
                <option key={type} value={type}>{type}</option>
              ))}
            </select>
          </label>
          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>Follow-up at (ISO)</span>
            <input
              className={styles.input}
              value={followUpAt}
              onChange={(event) => setFollowUpAt(event.target.value)}
              placeholder="2026-04-15T12:00:00Z"
              disabled={resolved || actionType !== "defer"}
            />
          </label>
        </div>

        {actionType === "merge" && canLoadMergeChoices ? (
          mergeRequiresUnmerge ? (
            <div className={styles.infoBanner}>This historical case includes a non-active person. You can view the comparison, but recreate/unmerge before submitting a new merge action.</div>
          ) : loadingChoices ? (
            <div className={styles.infoBanner}>Loading golden profile choices…</div>
          ) : choices.length > 0 ? (
            <GoldenProfilePicker
              choices={choices}
              selectedChoiceKeys={selectedChoiceKeys}
              leftPersonId={leftPersonId ?? ""}
              rightPersonId={rightPersonId ?? ""}
              onChange={(fieldName, choiceKey) =>
                setSelectedChoiceKeys((current) => ({ ...current, [fieldName]: choiceKey }))
              }
              disabled={resolved || actionBusy}
            />
          ) : (
            <div className={styles.infoBanner}>No golden profile choices available.</div>
          )
        ) : null}

        <label className={styles.fieldGroup}>
          <span className={styles.fieldLabel}>Notes</span>
          <textarea
            className={styles.textarea}
            value={notes}
            onChange={(event) => setNotes(event.target.value)}
            rows={4}
            disabled={resolved}
            placeholder="Reviewer notes"
          />
        </label>

        <button
          className={styles.primaryBtn}
          onClick={() => void onSubmitAction()}
          disabled={actionBusy || resolved || (actionType === "merge" && (!canLoadMergeChoices || mergeRequiresUnmerge))}
        >
          {actionBusy ? "Submitting…" : "Submit action"}
        </button>
      </div>
    </section>
  );
}

function selectedChoices(
  choices: readonly GoldenProfileChoice[],
  selectedChoiceKeys: Partial<Record<GoldenProfileFieldName, string>>,
): GoldenProfileChoice[] {
  const selectedKeys = new Set(Object.values(selectedChoiceKeys).filter((value) => value !== undefined));
  return choices.filter((choice) => selectedKeys.has(choice.key));
}
