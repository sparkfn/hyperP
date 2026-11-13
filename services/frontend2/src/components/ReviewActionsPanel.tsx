"use client";

import { useEffect, useState, type ReactElement } from "react";
import { z } from "zod";
import Link from "next/link";

import GoldenProfilePicker from "@/components/GoldenProfilePicker";
import { BffError, bffFetch } from "@/lib/api-client";
import type { Person } from "@/lib/api-types";
import {
  BULK_REVIEW_ACTION_TYPES,
  MAX_BULK_REVIEW_CASE_IDS,
  REVIEW_ACTION_REASON_CODES,
  REVIEW_ACTION_TYPES,
  isTerminalQueueState,
  type BulkReviewActionItemResult,
  type BulkReviewActionRequestBody,
  type BulkReviewActionType,
  type ReviewActionReasonCode,
  type ReviewActionRequestBody,
  type ReviewActionResponse,
  type ReviewActionType,
} from "@/lib/api-types-ops";
import {
  buildGoldenProfileChoices,
  loadGoldenProfileEvidence,
  selectionBody,
  type GoldenProfileChoice,
  type GoldenProfileFieldName,
} from "@/lib/golden-profile-choices";
import { formatDob } from "@/lib/display";
import { toBasePath } from "@/lib/route-paths";
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
  reviewCandidatePersonIds?: readonly string[];
  leftLabel?: string;
  rightLabel?: string;
  engineType?: string;
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

const REVIEW_ACTION_LABELS: Record<ReviewActionType, string> = {
  merge: 'Merge',
  reject: 'Reject (no match)',
  defer: 'Defer',
  escalate: 'Escalate',
  manual_no_match: 'Manual no-match',
};

const BULK_ACTION_LABELS: Record<BulkReviewActionType, string> = {
  resolve: "Resolve (no merge)",
  reject: "Reject (no match)",
  defer: "Defer",
  expire: "Expire",
  dismiss: "Dismiss",
};

const BULK_REASON_CODE_LABELS: Record<ReviewActionReasonCode, string> = {
  confirmed_match: "Confirmed match",
  confirmed_distinct: "Confirmed distinct",
  insufficient_evidence: "Insufficient evidence",
  parked_weak_signal: "Parked weak signal",
  stale_expired: "Stale / expired",
  awaiting_verification: "Awaiting verification",
  pair_audit_retired_in_v2: "Pair audit retired in v2",
  other: "Other",
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
  // assignedTo is received but currently unused — assign UI is hidden while the
  // feature is unused. Kept in the props so callers don't need to change.
  assignedTo: _assignedTo,
  leftPersonId,
  rightPersonId,
  leftPersonStatus,
  rightPersonStatus,
  defaultSurvivorPersonId,
  reviewCandidatePersonIds = [],
  leftLabel,
  rightLabel,
  engineType,
  onChanged,
  embedded = false,
  onActionBusy,
  onActionDone,
}: ReviewActionsPanelProps): ReactElement {
  // `assignedTo` is received (renamed to `_assignedTo` to avoid the unused-var
  // rule) but not used while the assign UI is hidden. Re-enable by restoring the
  // toggle button + assignInlineForm block + the onAssign handler.
  // The initial action type is derived from the engine once, on mount. It is
  // deliberately not re-derived while rendering: a mid-mount engine change
  // must not silently replace the reviewer's chosen action.
  const [actionType, setActionType] = useState<ReviewActionType>(() =>
    engineType === "pair_audit" ? "reject" : "merge",
  );
  const [reasonChoice, setReasonChoice] = useState<string>("");
  const [notes, setNotes] = useState<string>("");
  const reasonValid = reasonChoice !== "" && (reasonChoice !== OTHER_REASON || notes.trim() !== "");
  const [followUpAt, setFollowUpAt] = useState<string>("");
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const [loadingChoices, setLoadingChoices] = useState<boolean>(false);
  const [choices, setChoices] = useState<GoldenProfileChoice[]>([]);
  const [selectedChoiceKeys, setSelectedChoiceKeys] = useState<
    Partial<Record<GoldenProfileFieldName, string>>
  >({});
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [candidateProfiles, setCandidateProfiles] = useState<Record<string, Person>>({});
  const [candidateProfilesLoading, setCandidateProfilesLoading] = useState<boolean>(false);
  const [candidateProfilesError, setCandidateProfilesError] = useState<string | null>(null);
  const initialReviewCandidatePersonId =
    defaultSurvivorPersonId ?? rightPersonId ?? reviewCandidatePersonIds[0] ?? "";
  const [selectedReviewCandidatePersonId, setSelectedReviewCandidatePersonId] =
    useState<string>(initialReviewCandidatePersonId);

  const reviewCandidateKey = reviewCandidatePersonIds.join("\u001f");
  const selectedCandidateProfile = candidateProfiles[selectedReviewCandidatePersonId] ?? null;
  const candidateSelectionReady = reviewCandidatePersonIds.length === 0 || (
    !candidateProfilesLoading && selectedCandidateProfile !== null
  );
  const resolved = isTerminalQueueState(queueState);
  const isPairAudit = engineType === "pair_audit";
  // Pair-audit cases are never merged (the API rejects it). The reviewer must
  // pick an explicit non-merge action instead.
  const mergeDisabledForEngine = isPairAudit && actionType === "merge";
  const canLoadMergeChoices = leftPersonId !== null && rightPersonId !== null;
  const isPendingRecordReview =
    leftPersonId === null && selectedReviewCandidatePersonId.length > 0;
  const canSubmitMerge = (canLoadMergeChoices || isPendingRecordReview) && candidateSelectionReady;
  const selectedRightStatus = reviewCandidatePersonIds.length > 0
    ? selectedCandidateProfile?.status
    : rightPersonStatus;
  const mergeRequiresUnmerge = actionType === "merge" && (
    (leftPersonStatus !== undefined && leftPersonStatus !== null && leftPersonStatus !== "active") ||
    (selectedRightStatus !== undefined && selectedRightStatus !== null && selectedRightStatus !== "active")
  );
  const mergeSurvivorPersonId = reviewCandidatePersonIds.length > 0
    ? selectedReviewCandidatePersonId
    : defaultSurvivorPersonId != null
      && (defaultSurvivorPersonId === leftPersonId || defaultSurvivorPersonId === rightPersonId)
      ? defaultSurvivorPersonId
      : (rightPersonId ?? leftPersonId ?? "");

  useEffect(() => {
    let cancelled = false;
    const candidateIds = reviewCandidateKey === "" ? [] : reviewCandidateKey.split("\u001f");
    if (candidateIds.length === 0) {
      queueMicrotask(() => {
        setCandidateProfiles({});
        setCandidateProfilesError(null);
        setCandidateProfilesLoading(false);
      });
      return () => {
        cancelled = true;
      };
    }
    const loadCandidateProfiles = async (): Promise<void> => {
      setCandidateProfilesLoading(true);
      setCandidateProfilesError(null);
      try {
        const profiles = await Promise.all(
          candidateIds.map((personId) =>
            bffFetch<Person>(`/bff/persons/${encodeURIComponent(personId)}`),
          ),
        );
        if (cancelled) return;
        setCandidateProfiles(
          Object.fromEntries(profiles.map((profile) => [profile.person_id, profile])),
        );
        setSelectedReviewCandidatePersonId((current) =>
          candidateIds.includes(current) ? current : (candidateIds[0] ?? ""),
        );
      } catch (err: unknown) {
        if (cancelled) return;
        setCandidateProfiles({});
        setCandidateProfilesError(
          err instanceof BffError ? err.message : "Could not load CRM owner candidates.",
        );
      } finally {
        if (!cancelled) setCandidateProfilesLoading(false);
      }
    };
    void loadCandidateProfiles();
    return () => {
      cancelled = true;
    };
  }, [reviewCandidateKey]);

  useEffect(() => {
    if (
      actionType !== "merge" ||
      mergeDisabledForEngine ||
      !canLoadMergeChoices ||
      mergeRequiresUnmerge ||
      mergeSurvivorPersonId.length === 0
    ) {
      queueMicrotask(() => {
        setChoices([]);
        setSelectedChoiceKeys({});
        setLoadingChoices(false);
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
  }, [
    actionType,
    canLoadMergeChoices,
    leftPersonId,
    mergeDisabledForEngine,
    mergeSurvivorPersonId,
    mergeRequiresUnmerge,
    rightPersonId,
  ]);

  async function onSubmitAction(): Promise<void> {
    if (isPairAudit && actionType === "merge") {
      setError("Merge is disabled for pair-audit review cases.");
      return;
    }
    setError(null);
    setSuccess(null);
    if (!reasonValid) return;
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
        {/* Assign UI is disabled while the feature is unused.
            Re-enable by restoring the toggle button + assignInlineForm block. */}
      </div>
      {error !== null ? <div className={styles.errorBanner}>{error}</div> : null}
      {success !== null ? <div className={styles.successBanner}>{success}</div> : null}
      {resolved ? <div className={styles.infoBanner}>This review case is {queueState} and no longer accepts actions.</div> : null}
      {isPairAudit ? <div className={styles.infoBanner}>Merge is disabled for pair-audit cases.</div> : null}

      <div className={styles.actionSection}>
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
                <option key={type} value={type} disabled={type === "merge" && isPairAudit}>
                  {REVIEW_ACTION_LABELS[type]}
                </option>
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

        {actionType === "merge" && reviewCandidatePersonIds.length > 1 ? (
          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>CRM owner candidate</span>
            <select
              className={styles.select}
              value={selectedReviewCandidatePersonId}
              onChange={(event) => setSelectedReviewCandidatePersonId(event.target.value)}
              disabled={resolved || actionBusy}
            >
              {reviewCandidatePersonIds.map((personId) => {
                const profile = candidateProfiles[personId];
                const label = profile?.preferred_full_name?.trim() || personId;
                return <option key={personId} value={personId}>{label} - {personId}</option>;
              })}
            </select>
          </label>
        ) : null}

        {actionType === "merge" && reviewCandidatePersonIds.length > 0 ? (
          candidateProfilesLoading ? (
            <div className={styles.infoBanner}>Loading CRM owner profiles...</div>
          ) : candidateProfilesError !== null ? (
            <div className={styles.errorBanner}>{candidateProfilesError}</div>
          ) : selectedCandidateProfile !== null ? (
            <div className={styles.infoBanner}>
              <strong>{selectedCandidateProfile.preferred_full_name ?? "Unnamed person"}</strong>
              <div>Person ID: {selectedCandidateProfile.person_id}</div>
              <div>Status: {selectedCandidateProfile.status}</div>
              <div>Phone: {selectedCandidateProfile.preferred_phone ?? "-"}</div>
              <div>Email: {selectedCandidateProfile.preferred_email ?? "-"}</div>
              <div>DOB: {formatDob(selectedCandidateProfile.preferred_dob)}</div>
              <div>Address: {selectedCandidateProfile.preferred_address?.normalized_full ?? "-"}</div>
              <div>Source records: {selectedCandidateProfile.source_record_count}</div>
              <Link
                className={styles.profileLinkButton}
                href={toBasePath(`/persons/${encodeURIComponent(selectedCandidateProfile.person_id)}`)}
                target="_blank"
              >
                Inspect selected profile
              </Link>
            </div>
          ) : null
        ) : null}

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
              leftLabel={leftLabel}
              rightLabel={rightLabel}
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
          type="button"
          className={styles.primaryBtn}
          onClick={() => void onSubmitAction()}
          disabled={
            actionBusy ||
            resolved ||
            !reasonValid ||
            mergeDisabledForEngine ||
            (actionType === "merge" && (!canSubmitMerge || mergeRequiresUnmerge))
          }
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

// ---------------------------------------------------------------------------
// Bulk review actions
// ---------------------------------------------------------------------------

export interface BulkReviewActionsPanelProps {
  selectedReviewCaseIds: readonly string[];
  onSelectionChange: (remainingIds: string[]) => void;
  /** Awaited after a bulk action; must resolve only once the queue is reloaded. */
  onCompleted: () => Promise<void>;
  /**
   * Optional: called when a bulk request starts and ends so the parent can
   * lock the selection while the request is in flight — that way a newer
   * selection is never discarded when the outcome is applied.
   */
  onBusyChange?: (busy: boolean) => void;
}

// Mirrors BulkReviewActionItemResult: every field is required and nullable.
const bulkActionItemSchema = z.object({
  review_case_id: z.string().min(1),
  status: z.enum(["updated", "error"]),
  queue_state: z.string().nullable(),
  resolution: z.string().nullable(),
  error_code: z.string().nullable(),
});

const bulkActionItemsSchema = z.array(bulkActionItemSchema);

const bulkActionResponseSchema = z.object({ items: bulkActionItemsSchema });

/**
 * Parse and validate the bulk outcome payload. The outcome fields must agree
 * with the reported status: an updated outcome names the resulting queue state
 * and carries no error code, a failed outcome carries an error code and no
 * state. Returns null when the payload does not match the contract, so an
 * unusable response is never reported as a successful bulk action.
 */
function parseBulkActionItems(payload: unknown): BulkReviewActionItemResult[] | null {
  const parsed = bulkActionResponseSchema.safeParse(payload);
  if (!parsed.success) return null;
  const items: BulkReviewActionItemResult[] = [];
  for (const item of parsed.data.items) {
    if (item.status === "updated") {
      // An updated outcome must name the resulting queue state: an empty
      // state cannot describe a successful transition.
      if (item.queue_state === null || item.queue_state.trim().length === 0) return null;
      if (item.error_code !== null) return null;
    } else if (item.queue_state !== null || item.resolution !== null || !item.error_code) {
      return null;
    }
    items.push(item);
  }
  return items;
}

/**
 * Index the outcomes by review case id. Returns null unless every submitted id
 * has exactly one outcome and no outcome refers to an unsubmitted id.
 */
function indexBulkOutcomes(
  items: readonly BulkReviewActionItemResult[],
  submittedIds: readonly string[],
): Map<string, BulkReviewActionItemResult> | null {
  const outcomes = new Map<string, BulkReviewActionItemResult>();
  for (const item of items) {
    if (outcomes.has(item.review_case_id)) return null;
    outcomes.set(item.review_case_id, item);
  }
  if (outcomes.size !== submittedIds.length) return null;
  for (const id of submittedIds) {
    if (!outcomes.has(id)) return null;
  }
  return outcomes;
}

export function BulkReviewActionsPanel({
  selectedReviewCaseIds,
  onSelectionChange,
  onCompleted,
  onBusyChange,
}: BulkReviewActionsPanelProps): ReactElement {
  const [actionType, setActionType] = useState<BulkReviewActionType>("resolve");
  const [reasonCode, setReasonCode] = useState<ReviewActionReasonCode | "">("");
  const [notes, setNotes] = useState<string>("");
  const [followUpAt, setFollowUpAt] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [success, setSuccess] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Ids whose commit outcome is unknown: retrying them before the queue has
  // been reloaded could apply the action twice.
  const [unknownOutcomeIds, setUnknownOutcomeIds] = useState<string[]>([]);
  const [refreshFailed, setRefreshFailed] = useState<boolean>(false);

  const selectedCount = selectedReviewCaseIds.length;
  const blockedIds = unknownOutcomeIds.filter((id) => selectedReviewCaseIds.includes(id));
  const retryBlocked = blockedIds.length > 0 && refreshFailed;
  const reasonCodeValid = reasonCode !== "";
  const overLimit = selectedCount > MAX_BULK_REVIEW_CASE_IDS;

  async function reloadQueue(): Promise<boolean> {
    try {
      await onCompleted();
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Record the ids whose commit outcome is still unverified. A successful
   * reload resolves them; a failed reload leaves them blocked from retry.
   */
  function trackUnknownOutcome(ids: readonly string[], refreshed: boolean): void {
    setUnknownOutcomeIds((previous) =>
      refreshed ? [] : [...new Set([...previous, ...ids])],
    );
  }

  /** The response was unusable: the commits may have landed, so treat every id as unknown. */
  async function reportUnknownOutcome(ids: readonly string[], detail: string): Promise<void> {
    const refreshed = await reloadQueue();
    setRefreshFailed(!refreshed);
    trackUnknownOutcome(ids, refreshed);
    setSuccess(null);
    const messageDetail = detail.trimEnd();
    const sentenceSeparator = /[.!?]$/.test(messageDetail) ? " " : ". ";
    setError(
      `${messageDetail}${sentenceSeparator}The commit outcome for ${ids.length} submitted review case(s) is unknown. ` +
        (refreshed
          ? "The review queue was reloaded — verify these review cases before retrying."
          : "The review queue could not be reloaded — reload it before retrying."),
    );
    // Keep the submitted ids selected so the reviewer can verify and retry them.
    onSelectionChange([...ids]);
  }

  async function reportOutcomes(
    submittedIds: readonly string[],
    outcomes: Map<string, BulkReviewActionItemResult>,
  ): Promise<void> {
    const succeeded = new Set<string>();
    const failed: string[] = [];
    const unknown: string[] = [];
    const failedCodes: string[] = [];
    for (const id of submittedIds) {
      const item = outcomes.get(id);
      if (item === undefined) continue;
      if (item.status === "updated") {
        succeeded.add(id);
      } else if (item.error_code === "outcome_unknown") {
        unknown.push(id);
      } else {
        failed.push(id);
        failedCodes.push(item.error_code ?? "error");
      }
    }

    const refreshed = await reloadQueue();
    setRefreshFailed(!refreshed);
    trackUnknownOutcome(unknown, refreshed);

    const parts: string[] = [];
    if (succeeded.size > 0) {
      parts.push(`${succeeded.size} of ${submittedIds.length} review case(s) updated.`);
    }
    if (failed.length > 0) {
      const codes = [...new Set(failedCodes)].join(", ");
      parts.push(`${failed.length} review case(s) failed (${codes}).`);
    }
    if (unknown.length > 0) {
      parts.push(
        refreshed
          ? `Commit outcome is unknown for ${unknown.length} review case(s). The queue has been refreshed — verify them before retrying.`
          : `Commit outcome is unknown for ${unknown.length} review case(s) and the queue could not be refreshed. Do not retry them until the queue reloads.`,
      );
    }
    if (parts.length === 0) {
      parts.push("No review case was updated.");
    }
    if (!refreshed) {
      parts.push("The review queue could not be refreshed — reload it to see the current state.");
    }
    if (failed.length > 0 || unknown.length > 0 || !refreshed) {
      setError(parts.join(" "));
    } else {
      setSuccess(parts.join(" "));
    }
    // Keep the ids that were not applied selected, in submitted order, so the
    // reviewer can retry exactly the cases that did not go through.
    onSelectionChange(submittedIds.filter((id) => !succeeded.has(id)));
  }

  async function onSubmitBulk(): Promise<void> {
    if (busy) return;
    if (retryBlocked) {
      setError(
        "The review queue could not be reloaded after the last bulk action. Reload the queue before retrying.",
      );
      return;
    }
    if (selectedCount === 0) return;
    if (overLimit) {
      setError(`Bulk actions accept at most ${MAX_BULK_REVIEW_CASE_IDS} review cases at a time.`);
      return;
    }
    if (reasonCode === "") {
      setError("Select a reason code before applying a bulk action.");
      return;
    }
    const submittedIds = [...new Set(selectedReviewCaseIds)];
    setBusy(true);
    onBusyChange?.(true);
    setError(null);
    setSuccess(null);
    try {
      const body: BulkReviewActionRequestBody = {
        review_case_ids: submittedIds,
        action_type: actionType,
        reason_code: reasonCode,
        notes: notes.trim().length > 0 ? notes.trim() : null,
      };
      // A follow-up date only means something for a deferral. Sending it with
      // any other action would write a stale date onto a case that is being
      // closed out, so the field is omitted entirely outside a deferral.
      if (actionType === "defer" && followUpAt.trim().length > 0) {
        body.follow_up_at = followUpAt.trim();
      }
      const payload: unknown = await bffFetch<unknown>("/bff/review-cases/bulk-actions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const items = parseBulkActionItems(payload);
      const outcomes = items === null ? null : indexBulkOutcomes(items, submittedIds);
      if (outcomes === null) {
        await reportUnknownOutcome(
          submittedIds,
          "The bulk action response was invalid or incomplete.",
        );
        return;
      }
      await reportOutcomes(submittedIds, outcomes);
    } catch (err: unknown) {
      const detail = err instanceof BffError || err instanceof Error ? err.message : "Bulk action failed.";
      // The request may have committed before the transport failed.
      await reportUnknownOutcome(submittedIds, detail);
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  }

  async function onReloadQueue(): Promise<void> {
    setBusy(true);
    onBusyChange?.(true);
    try {
      await onCompleted();
      setRefreshFailed(false);
      setUnknownOutcomeIds([]);
      setError(null);
    } catch (err: unknown) {
      setRefreshFailed(true);
      setError(err instanceof BffError || err instanceof Error ? err.message : "Could not reload the review queue.");
    } finally {
      setBusy(false);
      onBusyChange?.(false);
    }
  }

  if (selectedCount === 0 && success === null && error === null && !refreshFailed) {
    return <></>;
  }

  return (
    <section className={styles.detailCard}>
      <div className={styles.cardHeaderRow}>
        <div className={styles.cardHeader}>Bulk actions</div>
        <span className={styles.muted}>{selectedCount} selected</span>
      </div>
      {error !== null ? <div className={styles.errorBanner}>{error}</div> : null}
      {success !== null ? (
        <div className={styles.successBanner}>
          {success}
          <button type="button" className={styles.clearBtn} onClick={() => setSuccess(null)}>Dismiss</button>
        </div>
      ) : null}
      {refreshFailed ? (
        <div className={styles.infoBanner}>
          {blockedIds.length > 0
            ? `Commit outcome is unknown for ${blockedIds.length} selected review case(s). Reload the queue before retrying.`
            : "The review queue could not be refreshed. Reload it to see the current state."}
          <button type="button" className={styles.clearBtn} onClick={() => void onReloadQueue()} disabled={busy}>
            Reload queue
          </button>
        </div>
      ) : null}

      {selectedCount === 0 ? null : (
        <div className={styles.actionSection}>
          <div className={styles.formRow}>
            <label className={styles.fieldGroup}>
              <span className={styles.fieldLabel}>Bulk action</span>
              <select
                className={styles.select}
                value={actionType}
                onChange={(event) => {
                  if (isBulkReviewActionType(event.target.value)) setActionType(event.target.value);
                }}
                disabled={busy}
              >
                {BULK_REVIEW_ACTION_TYPES.map((type) => (
                  <option key={type} value={type}>{BULK_ACTION_LABELS[type]}</option>
                ))}
              </select>
            </label>
            <label className={styles.fieldGroup}>
              <span className={styles.fieldLabel}>Reason code</span>
              <select
                className={styles.select}
                value={reasonCode}
                onChange={(event) => {
                  if (isReviewActionReasonCode(event.target.value)) setReasonCode(event.target.value);
                }}
                disabled={busy}
              >
                <option value="">Select a reason code…</option>
                {REVIEW_ACTION_REASON_CODES.map((code) => (
                  <option key={code} value={code}>{BULK_REASON_CODE_LABELS[code]}</option>
                ))}
              </select>
            </label>
          </div>

          {actionType === "defer" ? (
            <label className={styles.fieldGroup}>
              <span className={styles.fieldLabel}>Follow-up at (ISO)</span>
              <input
                className={styles.input}
                value={followUpAt}
                onChange={(event) => setFollowUpAt(event.target.value)}
                placeholder="2026-04-15T12:00:00Z"
                disabled={busy}
              />
            </label>
          ) : null}

          <label className={styles.fieldGroup}>
            <span className={styles.fieldLabel}>Note</span>
            <textarea
              className={styles.textarea}
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              rows={2}
              disabled={busy}
              placeholder="Optional note recorded with the action"
            />
          </label>

          <button
            type="button"
            className={styles.primaryBtn}
            onClick={() => void onSubmitBulk()}
            disabled={busy || overLimit || !reasonCodeValid || retryBlocked}
          >
            {busy ? "Applying…" : `Apply to ${selectedCount} review case(s)`}
          </button>
          {overLimit ? (
            <div className={styles.infoBanner}>
              Select at most {MAX_BULK_REVIEW_CASE_IDS} review cases per bulk action.
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

function isBulkReviewActionType(value: string): value is BulkReviewActionType {
  return (BULK_REVIEW_ACTION_TYPES as readonly string[]).includes(value);
}

function isReviewActionReasonCode(value: string): value is ReviewActionReasonCode {
  return (REVIEW_ACTION_REASON_CODES as readonly string[]).includes(value);
}
