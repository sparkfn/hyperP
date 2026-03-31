-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "citext";

-- CreateExtension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- CreateEnum
CREATE TYPE "PersonStatus" AS ENUM ('active', 'merged', 'suppressed');

-- CreateEnum
CREATE TYPE "RecordLinkStatus" AS ENUM ('linked', 'pending_review', 'rejected', 'suppressed');

-- CreateEnum
CREATE TYPE "IdentifierType" AS ENUM ('government_id_hash', 'phone', 'email', 'external_customer_id', 'membership_id', 'crm_contact_id', 'loyalty_id', 'custom');

-- CreateEnum
CREATE TYPE "TrustTier" AS ENUM ('tier_1', 'tier_2', 'tier_3', 'tier_4');

-- CreateEnum
CREATE TYPE "QualityFlag" AS ENUM ('valid', 'invalid_format', 'placeholder_value', 'shared_identifier_suspected', 'stale', 'source_untrusted');

-- CreateEnum
CREATE TYPE "MatchEngineType" AS ENUM ('deterministic', 'heuristic', 'llm', 'manual');

-- CreateEnum
CREATE TYPE "MatchDecisionType" AS ENUM ('merge', 'review', 'no_match');

-- CreateEnum
CREATE TYPE "ReviewQueueState" AS ENUM ('open', 'assigned', 'deferred', 'resolved', 'cancelled');

-- CreateEnum
CREATE TYPE "ReviewResolutionType" AS ENUM ('merge', 'reject', 'manual_no_match', 'cancelled_superseded');

-- CreateEnum
CREATE TYPE "MergeEventType" AS ENUM ('person_created', 'auto_merge', 'manual_merge', 'review_reject', 'manual_no_match', 'unmerge', 'person_split', 'survivorship_override');

-- CreateEnum
CREATE TYPE "ActorType" AS ENUM ('system', 'reviewer', 'admin', 'service');

-- CreateEnum
CREATE TYPE "LockType" AS ENUM ('manual_no_match', 'manual_merge_hint', 'person_suppression');

-- CreateEnum
CREATE TYPE "ReviewActionType" AS ENUM ('assign', 'unassign', 'merge', 'reject', 'manual_no_match', 'defer', 'escalate', 'cancel', 'reopen');

-- CreateTable
CREATE TABLE "source_system" (
    "source_system_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "source_key" TEXT NOT NULL,
    "display_name" TEXT NOT NULL,
    "system_type" TEXT NOT NULL,
    "is_active" BOOLEAN NOT NULL DEFAULT true,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_system_pkey" PRIMARY KEY ("source_system_id")
);

-- CreateTable
CREATE TABLE "source_field_trust" (
    "source_field_trust_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "source_system_id" UUID NOT NULL,
    "field_name" TEXT NOT NULL,
    "trust_tier" "TrustTier" NOT NULL,
    "notes" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_field_trust_pkey" PRIMARY KEY ("source_field_trust_id")
);

-- CreateTable
CREATE TABLE "ingest_run" (
    "ingest_run_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "source_system_id" UUID NOT NULL,
    "run_type" TEXT NOT NULL,
    "status" TEXT NOT NULL,
    "started_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "finished_at" TIMESTAMPTZ,
    "metadata" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "ingest_run_pkey" PRIMARY KEY ("ingest_run_id")
);

-- CreateTable
CREATE TABLE "person" (
    "person_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "status" "PersonStatus" NOT NULL DEFAULT 'active',
    "primary_source_system_id" UUID,
    "merged_into_person_id" UUID,
    "merge_lineage" TEXT,
    "is_high_value" BOOLEAN NOT NULL DEFAULT false,
    "is_high_risk" BOOLEAN NOT NULL DEFAULT false,
    "suppression_reason" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "person_pkey" PRIMARY KEY ("person_id")
);

-- CreateTable
CREATE TABLE "person_alias" (
    "person_alias_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "person_id" UUID NOT NULL,
    "alias_namespace" TEXT NOT NULL,
    "alias_value" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "person_alias_pkey" PRIMARY KEY ("person_alias_id")
);

-- CreateTable
CREATE TABLE "source_record" (
    "source_record_pk" UUID NOT NULL DEFAULT gen_random_uuid(),
    "source_system_id" UUID NOT NULL,
    "source_record_id" TEXT NOT NULL,
    "source_record_version" TEXT,
    "ingest_run_id" UUID,
    "linked_person_id" UUID,
    "link_status" "RecordLinkStatus" NOT NULL DEFAULT 'pending_review',
    "observed_at" TIMESTAMPTZ,
    "ingested_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "record_hash" TEXT NOT NULL,
    "raw_payload" JSONB NOT NULL,
    "normalized_payload" JSONB NOT NULL DEFAULT '{}',
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "retention_expires_at" TIMESTAMPTZ,

    CONSTRAINT "source_record_pkey" PRIMARY KEY ("source_record_pk")
);

-- CreateTable
CREATE TABLE "source_record_rejection" (
    "source_record_rejection_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "source_system_id" UUID NOT NULL,
    "source_record_id" TEXT,
    "ingest_run_id" UUID,
    "rejection_reason" TEXT NOT NULL,
    "raw_payload" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "source_record_rejection_pkey" PRIMARY KEY ("source_record_rejection_id")
);

-- CreateTable
CREATE TABLE "person_identifier" (
    "person_identifier_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "person_id" UUID NOT NULL,
    "source_record_pk" UUID,
    "source_system_id" UUID NOT NULL,
    "identifier_type" "IdentifierType" NOT NULL,
    "raw_value" TEXT,
    "normalized_value" TEXT,
    "hashed_value" TEXT,
    "is_verified" BOOLEAN NOT NULL DEFAULT false,
    "verification_method" TEXT,
    "is_active" BOOLEAN NOT NULL DEFAULT true,
    "quality_flag" "QualityFlag" NOT NULL DEFAULT 'valid',
    "first_seen_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_seen_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "last_confirmed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "metadata" JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT "person_identifier_pkey" PRIMARY KEY ("person_identifier_id")
);

-- CreateTable
CREATE TABLE "person_attribute_fact" (
    "person_attribute_fact_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "person_id" UUID NOT NULL,
    "source_record_pk" UUID,
    "source_system_id" UUID NOT NULL,
    "attribute_name" TEXT NOT NULL,
    "attribute_value" JSONB NOT NULL,
    "source_trust_tier" "TrustTier" NOT NULL,
    "confidence" DECIMAL(5,4) NOT NULL DEFAULT 1.0,
    "quality_flag" "QualityFlag" NOT NULL DEFAULT 'valid',
    "is_current_hint" BOOLEAN NOT NULL DEFAULT false,
    "observed_at" TIMESTAMPTZ NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "person_attribute_fact_pkey" PRIMARY KEY ("person_attribute_fact_id")
);

-- CreateTable
CREATE TABLE "survivorship_override" (
    "survivorship_override_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "person_id" UUID NOT NULL,
    "attribute_name" TEXT NOT NULL,
    "selected_person_attribute_fact_id" UUID NOT NULL,
    "reason" TEXT NOT NULL,
    "actor_type" "ActorType" NOT NULL,
    "actor_id" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "survivorship_override_pkey" PRIMARY KEY ("survivorship_override_id")
);

-- CreateTable
CREATE TABLE "golden_profile" (
    "person_id" UUID NOT NULL,
    "preferred_full_name" TEXT,
    "preferred_phone" TEXT,
    "preferred_email" TEXT,
    "preferred_dob" DATE,
    "preferred_address" JSONB,
    "profile_completeness_score" DECIMAL(5,4) NOT NULL DEFAULT 0,
    "computed_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "computation_version" TEXT NOT NULL,

    CONSTRAINT "golden_profile_pkey" PRIMARY KEY ("person_id")
);

-- CreateTable
CREATE TABLE "golden_profile_lineage" (
    "golden_profile_lineage_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "person_id" UUID NOT NULL,
    "field_name" TEXT NOT NULL,
    "person_attribute_fact_id" UUID,
    "person_identifier_id" UUID,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "golden_profile_lineage_pkey" PRIMARY KEY ("golden_profile_lineage_id")
);

-- CreateTable
CREATE TABLE "candidate_pair" (
    "candidate_pair_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "left_entity_type" TEXT NOT NULL,
    "left_entity_id" TEXT NOT NULL,
    "right_entity_type" TEXT NOT NULL,
    "right_entity_id" TEXT NOT NULL,
    "blocking_reason" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "candidate_pair_pkey" PRIMARY KEY ("candidate_pair_id")
);

-- CreateTable
CREATE TABLE "match_decision" (
    "match_decision_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "left_entity_type" TEXT NOT NULL,
    "left_entity_id" TEXT NOT NULL,
    "right_entity_type" TEXT NOT NULL,
    "right_entity_id" TEXT NOT NULL,
    "candidate_pair_id" UUID,
    "engine_type" "MatchEngineType" NOT NULL,
    "engine_version" TEXT NOT NULL,
    "decision" "MatchDecisionType" NOT NULL,
    "confidence" DECIMAL(5,4),
    "reasons" JSONB NOT NULL DEFAULT '[]',
    "blocking_conflicts" JSONB NOT NULL DEFAULT '[]',
    "feature_snapshot" JSONB NOT NULL DEFAULT '{}',
    "prompt_snapshot" JSONB,
    "policy_version" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "retention_expires_at" TIMESTAMPTZ,

    CONSTRAINT "match_decision_pkey" PRIMARY KEY ("match_decision_id")
);

-- CreateTable
CREATE TABLE "person_pair_lock" (
    "person_pair_lock_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "left_person_id" UUID,
    "right_person_id" UUID,
    "left_source_record_pk" UUID,
    "right_source_record_pk" UUID,
    "lock_type" "LockType" NOT NULL,
    "reason" TEXT NOT NULL,
    "expires_at" TIMESTAMPTZ,
    "actor_type" "ActorType" NOT NULL,
    "actor_id" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "person_pair_lock_pkey" PRIMARY KEY ("person_pair_lock_id")
);

-- CreateTable
CREATE TABLE "review_case" (
    "review_case_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "match_decision_id" UUID NOT NULL,
    "priority" INTEGER NOT NULL DEFAULT 100,
    "queue_state" "ReviewQueueState" NOT NULL DEFAULT 'open',
    "assigned_to" TEXT,
    "follow_up_at" TIMESTAMPTZ,
    "sla_due_at" TIMESTAMPTZ,
    "resolution" "ReviewResolutionType",
    "resolved_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "review_case_pkey" PRIMARY KEY ("review_case_id")
);

-- CreateTable
CREATE TABLE "review_action" (
    "review_action_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "review_case_id" UUID NOT NULL,
    "action_type" "ReviewActionType" NOT NULL,
    "actor_type" "ActorType" NOT NULL,
    "actor_id" TEXT NOT NULL,
    "notes" TEXT,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "retention_expires_at" TIMESTAMPTZ,

    CONSTRAINT "review_action_pkey" PRIMARY KEY ("review_action_id")
);

-- CreateTable
CREATE TABLE "merge_event" (
    "merge_event_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "event_type" "MergeEventType" NOT NULL,
    "from_person_id" UUID,
    "to_person_id" UUID,
    "match_decision_id" UUID,
    "actor_type" "ActorType" NOT NULL,
    "actor_id" TEXT NOT NULL,
    "reason" TEXT NOT NULL,
    "metadata" JSONB NOT NULL DEFAULT '{}',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "retention_expires_at" TIMESTAMPTZ,

    CONSTRAINT "merge_event_pkey" PRIMARY KEY ("merge_event_id")
);

-- CreateTable
CREATE TABLE "merge_event_source_record" (
    "merge_event_source_record_id" UUID NOT NULL DEFAULT gen_random_uuid(),
    "merge_event_id" UUID NOT NULL,
    "source_record_pk" UUID NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "merge_event_source_record_pkey" PRIMARY KEY ("merge_event_source_record_id")
);

-- CreateIndex
CREATE UNIQUE INDEX "source_system_source_key_key" ON "source_system"("source_key");

-- CreateIndex
CREATE UNIQUE INDEX "source_field_trust_source_system_id_field_name_key" ON "source_field_trust"("source_system_id", "field_name");

-- CreateIndex
CREATE UNIQUE INDEX "person_alias_alias_namespace_alias_value_key" ON "person_alias"("alias_namespace", "alias_value");

-- CreateIndex
CREATE UNIQUE INDEX "source_record_source_system_id_source_record_id_record_hash_key" ON "source_record"("source_system_id", "source_record_id", "record_hash");

-- CreateIndex
CREATE UNIQUE INDEX "survivorship_override_person_id_attribute_name_key" ON "survivorship_override"("person_id", "attribute_name");

-- CreateIndex
CREATE UNIQUE INDEX "golden_profile_lineage_person_id_field_name_key" ON "golden_profile_lineage"("person_id", "field_name");

-- CreateIndex
CREATE UNIQUE INDEX "candidate_pair_left_entity_type_left_entity_id_right_entity_key" ON "candidate_pair"("left_entity_type", "left_entity_id", "right_entity_type", "right_entity_id", "blocking_reason");

-- CreateIndex
CREATE UNIQUE INDEX "merge_event_source_record_merge_event_id_source_record_pk_key" ON "merge_event_source_record"("merge_event_id", "source_record_pk");

-- AddForeignKey
ALTER TABLE "source_field_trust" ADD CONSTRAINT "source_field_trust_source_system_id_fkey" FOREIGN KEY ("source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ingest_run" ADD CONSTRAINT "ingest_run_source_system_id_fkey" FOREIGN KEY ("source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person" ADD CONSTRAINT "person_primary_source_system_id_fkey" FOREIGN KEY ("primary_source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person" ADD CONSTRAINT "person_merged_into_person_id_fkey" FOREIGN KEY ("merged_into_person_id") REFERENCES "person"("person_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_alias" ADD CONSTRAINT "person_alias_person_id_fkey" FOREIGN KEY ("person_id") REFERENCES "person"("person_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_record" ADD CONSTRAINT "source_record_source_system_id_fkey" FOREIGN KEY ("source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_record" ADD CONSTRAINT "source_record_ingest_run_id_fkey" FOREIGN KEY ("ingest_run_id") REFERENCES "ingest_run"("ingest_run_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_record" ADD CONSTRAINT "source_record_linked_person_id_fkey" FOREIGN KEY ("linked_person_id") REFERENCES "person"("person_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "source_record_rejection" ADD CONSTRAINT "source_record_rejection_source_system_id_fkey" FOREIGN KEY ("source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_identifier" ADD CONSTRAINT "person_identifier_person_id_fkey" FOREIGN KEY ("person_id") REFERENCES "person"("person_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_identifier" ADD CONSTRAINT "person_identifier_source_record_pk_fkey" FOREIGN KEY ("source_record_pk") REFERENCES "source_record"("source_record_pk") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_identifier" ADD CONSTRAINT "person_identifier_source_system_id_fkey" FOREIGN KEY ("source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_attribute_fact" ADD CONSTRAINT "person_attribute_fact_person_id_fkey" FOREIGN KEY ("person_id") REFERENCES "person"("person_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_attribute_fact" ADD CONSTRAINT "person_attribute_fact_source_record_pk_fkey" FOREIGN KEY ("source_record_pk") REFERENCES "source_record"("source_record_pk") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_attribute_fact" ADD CONSTRAINT "person_attribute_fact_source_system_id_fkey" FOREIGN KEY ("source_system_id") REFERENCES "source_system"("source_system_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "survivorship_override" ADD CONSTRAINT "survivorship_override_person_id_fkey" FOREIGN KEY ("person_id") REFERENCES "person"("person_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "survivorship_override" ADD CONSTRAINT "survivorship_override_selected_person_attribute_fact_id_fkey" FOREIGN KEY ("selected_person_attribute_fact_id") REFERENCES "person_attribute_fact"("person_attribute_fact_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "golden_profile" ADD CONSTRAINT "golden_profile_person_id_fkey" FOREIGN KEY ("person_id") REFERENCES "person"("person_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "golden_profile_lineage" ADD CONSTRAINT "golden_profile_lineage_person_id_fkey" FOREIGN KEY ("person_id") REFERENCES "person"("person_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "golden_profile_lineage" ADD CONSTRAINT "golden_profile_lineage_person_attribute_fact_id_fkey" FOREIGN KEY ("person_attribute_fact_id") REFERENCES "person_attribute_fact"("person_attribute_fact_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "golden_profile_lineage" ADD CONSTRAINT "golden_profile_lineage_person_identifier_id_fkey" FOREIGN KEY ("person_identifier_id") REFERENCES "person_identifier"("person_identifier_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "match_decision" ADD CONSTRAINT "match_decision_candidate_pair_id_fkey" FOREIGN KEY ("candidate_pair_id") REFERENCES "candidate_pair"("candidate_pair_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_pair_lock" ADD CONSTRAINT "person_pair_lock_left_person_id_fkey" FOREIGN KEY ("left_person_id") REFERENCES "person"("person_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_pair_lock" ADD CONSTRAINT "person_pair_lock_right_person_id_fkey" FOREIGN KEY ("right_person_id") REFERENCES "person"("person_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_pair_lock" ADD CONSTRAINT "person_pair_lock_left_source_record_pk_fkey" FOREIGN KEY ("left_source_record_pk") REFERENCES "source_record"("source_record_pk") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "person_pair_lock" ADD CONSTRAINT "person_pair_lock_right_source_record_pk_fkey" FOREIGN KEY ("right_source_record_pk") REFERENCES "source_record"("source_record_pk") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "review_case" ADD CONSTRAINT "review_case_match_decision_id_fkey" FOREIGN KEY ("match_decision_id") REFERENCES "match_decision"("match_decision_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "review_action" ADD CONSTRAINT "review_action_review_case_id_fkey" FOREIGN KEY ("review_case_id") REFERENCES "review_case"("review_case_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "merge_event" ADD CONSTRAINT "merge_event_from_person_id_fkey" FOREIGN KEY ("from_person_id") REFERENCES "person"("person_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "merge_event" ADD CONSTRAINT "merge_event_to_person_id_fkey" FOREIGN KEY ("to_person_id") REFERENCES "person"("person_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "merge_event" ADD CONSTRAINT "merge_event_match_decision_id_fkey" FOREIGN KEY ("match_decision_id") REFERENCES "match_decision"("match_decision_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "merge_event_source_record" ADD CONSTRAINT "merge_event_source_record_merge_event_id_fkey" FOREIGN KEY ("merge_event_id") REFERENCES "merge_event"("merge_event_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "merge_event_source_record" ADD CONSTRAINT "merge_event_source_record_source_record_pk_fkey" FOREIGN KEY ("source_record_pk") REFERENCES "source_record"("source_record_pk") ON DELETE RESTRICT ON UPDATE CASCADE;
