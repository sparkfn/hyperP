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
  defaultSurvivorPersonId?: string | null;
  onChanged: () => Promise<void>;
  embedded?: boolean;
  onActionBusy?: (busy: boolean) => void;
  onActionDone?: (success: boolean, message: string) => void;
}

function isReviewActionType(value: string): value is ReviewActionType {
  return (REVIEW_ACTION_TYPES as readonly string[]).includes(value);
}

const OTHER_REASON = "__other__";

const REVIEW_REASON_PRESETS: Record<ReviewActionType, readonly string[]> = {
  merge: [
    "Confirmed same person — matching government ID and name.",
    "Confirmed same person — matching phone/email and date of birth.",
    "Confirmed same person — supporting documentary evidence reviewed.",
  ],
  reject: [
    "Different people — conflicting identity details.",
    "Different people — name and date of birth mismatch.",
    "Insufficient evidence to merge these records.",
  ],
  defer: [
    "Awaiting additional information before deciding.",
    "Pending verification with the source system.",
    "Needs follow-up — flagged for later review.",
  ],
  escalate: [
    "Ambiguous case — needs a senior reviewer.",
    "Potential data-quality issue in the source record.",
    "Possible duplicate involving sensitive data.",
  ],
  manual_no_match: [
    "Confirmed not a match after manual review.",
    "Shared identifier but clearly distinct individuals.",
    "Coincidental identifier overlap (e.g. shared phone number).",
  ],
};

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
  defaultSurvivorPersonId,
  onChanged,
  embedded = false,
  onActionBusy,
  onActionDone,
}: ReviewActionsPanelProps): ReactElement {
  const [assignee, setAssignee] = useState<string>(assignedTo ?? "");
  const [assignBusy, setAssignBusy] = useState<boolean>(false);
  const [actionType, setActionType] = useState<ReviewActionType>("merge");
  const [reasonChoice, setReasonChoice] = useState<string>("");
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
  const [assignOpen, setAssignOpen] = useState<boolean>(false);

  const resolved = queueState === "resolved" || queueState === "cancelled";
  const canLoadMergeChoices = leftPersonId !== null && rightPersonId !== null;
  const mergeRequiresUnmerge = actionType === "merge" && (
    (leftPersonStatus !== undefined && leftPersonStatus !== null && leftPersonStatus !== "active") ||
    (rightPersonStatus !== undefined && rightPersonStatus !== null && rightPersonStatus !== "active")
  );
  const mergeSurvivorPersonId =
    defaultSurvivorPersonId != null &&
    (defaultSurvivorPersonId === leftPersonId || defaultSurvivorPersonId === rightPersonId)
      ? defaultSurvivorPersonId
      : (rightPersonId ?? leftPersonId ?? "");

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
    onActionBusy?.(true);
    try {
      const reasonText = reasonChoice === OTHER_REASON ? notes.trim() : reasonChoice;
      const body: ReviewActionRequestBody = {
        action_type: actionType,
        notes: reasonText.length > 0 ? reasonText : null,
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
      const msg = `${actionType === "merge" ? "Merged" : actionType === "reject" ? "Rejected" : actionType === "manual_no_match" ? "No-match recorded" : actionType === "defer" ? "Deferred" : "Escalated"} — state: ${result.queue_state}${result.resolution !== null ? ` (${result.resolution})` : ""}.`;
      setSuccess(msg);
      setReasonChoice("");
      setNotes("");
      setFollowUpAt("");
      onActionDone?.(true, msg);
      await onChanged();
    } catch (err: unknown) {
      const msg = err instanceof BffError || err instanceof Error ? err.message : "Action failed.";
      setError(msg);
      onActionDone?.(false, msg);
    } finally {
      setActionBusy(false);
      onActionBusy?.(false);
    }
  }

  return (
    <section className={`${styles.detailCard} ${embedded ? styles.embeddedActionPanel : ""}`}>
      <div className={styles.cardHeaderRow}>
        <div className={styles.cardHeader}>Reviewer Actions</div>
        {!resolved && !assignOpen && (
          <button
            type="button"
            className={styles.assignToggleBtn}
            onClick={() => setAssignOpen(true)}
          >
            {assignedTo !== null && assignedTo.length > 0 ? `Assigned: ${assignedTo}` : "Assign"}
          </button>
        )}
      </div>
      {error !== null ? <div className={styles.errorBanner}>{error}</div> : null}
      {success !== null ? <div className={styles.successBanner}>{success}</div> : null}
      {resolved ? <div className={styles.infoBanner}>This review case is {queueState} and no longer accepts actions.</div> : null}

      {assignOpen && (
        <div className={styles.assignInlineForm}>
          <div className={styles.assignInlineRow}>
            <input
              className={styles.input}
              value={assignee}
              onChange={(event) => setAssignee(event.target.value)}
              placeholder="Reviewer ID"
              disabled={resolved}
            />
            <button type="button" className={styles.primaryBtn} onClick={() => void onAssign()} disabled={assignBusy || resolved}>
              {assignBusy ? "…" : "Assign"}
            </button>
            <button type="button" className={styles.assignCancelBtn} onClick={() => setAssignOpen(false)}>✕</button>
          </div>
        </div>
      )}

      <div className={`${styles.actionSection} ${assignOpen ? styles.actionSectionDisabled : ""}`}>
        <div className={styles.sectionTitle}>Submit action</div>
        <div className={styles.formRow}>
          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>Action type</span>
            <select
              className={styles.select}
              value={actionType}
              onChange={(event) => {
                if (isReviewActionType(event.target.value)) {
                  setActionType(event.target.value);
                  setReasonChoice("");
                  setNotes("");
                }
              }}
              disabled={resolved}
            >
              {REVIEW_ACTION_TYPES.map((type) => (
                <option key={type} value={type}>{type}</option>
              ))}
            </select>
          </label>
          {actionType === "defer" && (
            <label className={styles.fieldGroup}>
              <span className={styles.fieldLabel}>Follow-up at (ISO)</span>
              <input
                className={styles.input}
                value={followUpAt}
                onChange={(event) => setFollowUpAt(event.target.value)}
                placeholder="2026-04-15T12:00:00Z"
                disabled={resolved}
              />
            </label>
          )}
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
          <span className={styles.fieldLabel}>Review note</span>
          <select
            className={styles.select}
            value={reasonChoice}
            onChange={(event) => setReasonChoice(event.target.value)}
            disabled={resolved}
          >
            <option value="">Select a reason…</option>
            {REVIEW_REASON_PRESETS[actionType].map((reason) => (
              <option key={reason} value={reason}>{reason}</option>
            ))}
            <option value={OTHER_REASON}>Other…</option>
          </select>
        </label>

        {reasonChoice === OTHER_REASON ? (
          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>Custom note</span>
            <textarea
              className={styles.textarea}
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              rows={3}
              disabled={resolved}
              placeholder="Enter reviewer notes"
            />
          </label>
        ) : null}

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
