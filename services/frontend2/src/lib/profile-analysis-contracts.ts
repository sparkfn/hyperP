import { z } from "zod";

import type {
  PersonProfileAnalyses,
  ProfileAnalysisCurrent,
  ProfileAnalysisHistoryItem,
  ProfileAnalysisSlot,
} from "./api-types-person";

const analysisTypeSchema = z.enum(["sales", "contact_tracing"]);
const nonEmptyStringSchema = z.string().refine((value) => value.trim().length > 0, {
  message: "String must not be blank.",
});
const timestampSchema = z.iso.datetime({ offset: true });
const failureCodeSchema = z.string().min(1).max(64).regex(
  /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/,
  "Failure code must be a safe lower-case identifier.",
);
const revisionSchema = z.number().int().nonnegative();
const attemptSchema = z.number().int().positive();

const provenanceShape = {
  input_revision: revisionSchema,
  input_fingerprint: nonEmptyStringSchema,
  prompt_version: nonEmptyStringSchema,
  provider: nonEmptyStringSchema,
  model: nonEmptyStringSchema,
  started_at: timestampSchema,
  completed_at: timestampSchema,
  completed_at_display: nonEmptyStringSchema,
  generated_age_display: nonEmptyStringSchema.optional().default("Unknown age"),
  valid_until: timestampSchema.optional().default("9999-12-31T23:59:59+00:00"),
  valid_until_display: nonEmptyStringSchema.optional().default("Unknown"),
  attempt_number: attemptSchema,
};

function addTimingIssue(
  value: { started_at: string; completed_at: string },
  context: z.RefinementCtx,
): void {
  if (Date.parse(value.completed_at) < Date.parse(value.started_at)) {
    context.addIssue({
      code: "custom",
      message: "Completion timestamp must not precede start timestamp.",
      path: ["completed_at"],
    });
  }
}

export const profileAnalysisCurrentSchema: z.ZodType<ProfileAnalysisCurrent> = z.object({
  analysis_id: nonEmptyStringSchema,
  person_id: nonEmptyStringSchema,
  analysis_type: analysisTypeSchema,
  status: z.literal("succeeded"),
  content: nonEmptyStringSchema,
  ...provenanceShape,
}).strict().superRefine(addTimingIssue);

export const profileAnalysisSlotSchema: z.ZodType<ProfileAnalysisSlot> = z.object({
  current: profileAnalysisCurrentSchema.nullable(),
  stale: z.boolean(),
  expired: z.boolean().optional().default(false),
  valid: z.boolean().optional().default(false),
  invalid_reason: z.enum(["missing", "stale", "expired", "stale_and_expired"]).nullable().optional().default(null),
  refresh_state: z.enum(["disabled", "idle", "pending", "running", "retrying", "ready", "failed"]),
  failure_code: failureCodeSchema.nullable(),
  auto_request_allowed: z.boolean().optional().default(false),
  next_retry_at: timestampSchema.nullable().optional().default(null),
  next_retry_at_display: nonEmptyStringSchema.nullable().optional().default(null),
  retry_allowed: z.boolean(),
  retry_attempts_remaining: z.number().int().min(0).max(3),
  retry_available_at: timestampSchema.nullable(),
  retry_available_at_display: nonEmptyStringSchema.nullable(),
  force_attempts_remaining: z.number().int().min(0).max(3).optional().default(3),
  force_available_at: timestampSchema.nullable().optional().default(null),
  force_available_at_display: nonEmptyStringSchema.nullable().optional().default(null),
}).strict();

export const personProfileAnalysesSchema: z.ZodType<PersonProfileAnalyses> = z.object({
  input_revision: revisionSchema,
  refresh_state: z.enum([
    "disabled", "pending", "running", "retrying", "ready", "partial", "failed",
  ]),
  sales: profileAnalysisSlotSchema,
  contact_tracing: profileAnalysisSlotSchema,
}).strict().superRefine((analyses, context) => {
  if (analyses.sales.current?.analysis_type === "contact_tracing") {
    context.addIssue({
      code: "custom",
      message: "Sales slot must contain a sales analysis.",
      path: ["sales", "current", "analysis_type"],
    });
  }
  if (analyses.contact_tracing.current?.analysis_type === "sales") {
    context.addIssue({
      code: "custom",
      message: "Contact-tracing slot must contain a contact-tracing analysis.",
      path: ["contact_tracing", "current", "analysis_type"],
    });
  }
  for (const [slotName, slot] of [
    ["sales", analyses.sales],
    ["contact_tracing", analyses.contact_tracing],
  ] as const) {
    const expectedStale = slot.current !== null
      && slot.current.input_revision !== analyses.input_revision;
    if (slot.stale !== expectedStale) {
      context.addIssue({
        code: "custom",
        message: "Stale flag must agree with the current analysis revision.",
        path: [slotName, "stale"],
      });
    }
    const valid = slot.current !== null && !expectedStale && !slot.expired;
    if (slot.refresh_state === "ready" && !valid) {
      context.addIssue({
        code: "custom",
        message: "A ready slot requires a successful current-revision analysis.",
        path: [slotName, "refresh_state"],
      });
    }
    if (valid && !["ready", "disabled"].includes(slot.refresh_state)) {
      context.addIssue({
        code: "custom",
        message: "A fresh analysis must be ready unless generation is disabled.",
        path: [slotName, "refresh_state"],
      });
    }
    if (slot.failure_code !== null && slot.refresh_state !== "failed") {
      context.addIssue({
        code: "custom",
        message: "Failure codes are valid only for failed slots.",
        path: [slotName, "failure_code"],
      });
    }
    if (slot.retry_allowed !== (slot.refresh_state === "failed"
      && slot.retry_attempts_remaining > 0)) {
      context.addIssue({
        code: "custom",
        message: "Retry eligibility must agree with failed state and the user retry budget.",
        path: [slotName, "retry_allowed"],
      });
    }
    if ((slot.retry_attempts_remaining === 0) !== (slot.retry_available_at !== null)) {
      context.addIssue({
        code: "custom",
        message: "Retry availability must be present exactly when the retry budget is exhausted.",
        path: [slotName, "retry_available_at"],
      });
    }
    if ((slot.retry_available_at === null) !== (slot.retry_available_at_display === null)) {
      context.addIssue({
        code: "custom",
        message: "Retry availability display must agree with its timestamp.",
        path: [slotName, "retry_available_at_display"],
      });
    }
  }

  const retryBudgetFields = [
    "retry_attempts_remaining",
    "retry_available_at",
    "retry_available_at_display",
  ] as const;
  for (const field of retryBudgetFields) {
    if (analyses.sales[field] !== analyses.contact_tracing[field]) {
      context.addIssue({
        code: "custom",
        message: "Both slots must expose the same per-Person user retry budget.",
        path: ["contact_tracing", field],
      });
    }
  }

  const states = [analyses.sales.refresh_state, analyses.contact_tracing.refresh_state];
  const disabledCount = states.filter((state) => state === "disabled").length;
  if (disabledCount === 1) {
    context.addIssue({
      code: "custom",
      message: "Generation is disabled for both analysis slots or neither.",
      path: ["refresh_state"],
    });
  }
  const expectedOverall = disabledCount === 2
    ? "disabled"
    : states.includes("running")
      ? "running"
      : states.includes("retrying")
        ? "retrying"
        : states.every((state) => state === "ready")
          ? "ready"
          : states.filter((state) => state === "ready").length === 1
            ? "partial"
            : states.includes("failed")
              ? "failed"
              : "pending";
  if (analyses.refresh_state !== expectedOverall) {
    context.addIssue({
      code: "custom",
      message: "Overall refresh state must follow slot-state precedence.",
      path: ["refresh_state"],
    });
  }
});

export const profileAnalysisHistoryItemSchema: z.ZodType<ProfileAnalysisHistoryItem> = z.object({
  analysis_id: nonEmptyStringSchema,
  person_id: nonEmptyStringSchema,
  analysis_type: analysisTypeSchema,
  status: z.enum(["succeeded", "failed", "obsolete"]),
  content: nonEmptyStringSchema.nullable(),
  ...provenanceShape,
  failure_code: failureCodeSchema.nullable(),
  retryable: z.boolean().nullable(),
  next_retry_at: timestampSchema.nullable(),
}).strict().superRefine((item, context) => {
  addTimingIssue(item, context);
  const failureMetadata = [item.failure_code, item.retryable, item.next_retry_at];
  if (item.status === "succeeded") {
    if (item.content === null) {
      context.addIssue({ code: "custom", message: "Success requires content.", path: ["content"] });
    }
    if (failureMetadata.some((value) => value !== null)) {
      context.addIssue({
        code: "custom",
        message: "Success cannot carry failure metadata.",
        path: ["failure_code"],
      });
    }
  } else if (item.status === "obsolete") {
    if (failureMetadata.some((value) => value !== null)) {
      context.addIssue({
        code: "custom",
        message: "Obsolete attempts cannot carry failure metadata.",
        path: ["failure_code"],
      });
    }
  } else {
    if (item.content !== null) {
      context.addIssue({ code: "custom", message: "Failure cannot carry content.", path: ["content"] });
    }
    if (item.retryable === null) {
      context.addIssue({
        code: "custom",
        message: "Failure requires retryability metadata.",
        path: ["retryable"],
      });
    } else if (item.retryable !== (item.next_retry_at !== null)) {
      context.addIssue({
        code: "custom",
        message: "Retry time must agree with retryability.",
        path: ["next_retry_at"],
      });
    }
  }
});

const profileAnalysisHistorySchema: z.ZodType<ProfileAnalysisHistoryItem[]> = z.array(
  profileAnalysisHistoryItemSchema,
);

export function parsePersonProfileAnalyses(
  value: unknown,
  expectedPersonId: string,
): PersonProfileAnalyses {
  return personProfileAnalysesSchema.superRefine((analyses, context) => {
    for (const [slotName, slot] of [
      ["sales", analyses.sales],
      ["contact_tracing", analyses.contact_tracing],
    ] as const) {
      if (slot.current !== null && slot.current.person_id !== expectedPersonId) {
        context.addIssue({
          code: "custom",
          message: "Current analysis belongs to another Person.",
          path: [slotName, "current", "person_id"],
        });
      }
    }
  }).parse(value);
}

export function parseProfileAnalysisHistory(value: unknown): ProfileAnalysisHistoryItem[] {
  return profileAnalysisHistorySchema.parse(value);
}
