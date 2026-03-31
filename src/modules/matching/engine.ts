import { prisma } from "../../common/db.js";
import type { IdentifierType, TrustTier, Prisma } from "../../generated/prisma/client.js";
import type { NormalizedIdentifier } from "../ingestion/normalizer.js";
import { generateCandidates, hasNoMatchLock } from "./candidate-generation.js";
import { evaluateDeterministicRules, checkNoMatchLock } from "./deterministic.js";
import { createPersonFromSourceRecord, mergePersons } from "../person/service.js";

export interface MatchEngineInput {
	sourceRecordPk: string;
	sourceSystemId: string;
	sourceKey: string;
	identifiers: NormalizedIdentifier[];
	attributes: Array<{
		attributeName: string;
		attributeValue: unknown;
		trustTier: TrustTier;
		observedAt: Date;
	}>;
	existingPersonId?: string | null;
}

export interface MatchEngineResult {
	personId: string;
	action: "created" | "merged" | "no_match" | "review";
	matchDecisionId?: string;
	mergeEventId?: string;
}

/**
 * Run the match engine for an ingested source record.
 *
 * Flow:
 * 1. Generate candidates via blocking keys
 * 2. If no candidates → create new person
 * 3. For each candidate:
 *    a. Check no-match locks
 *    b. Evaluate deterministic rules
 *    c. If hard merge → merge and return
 *    d. If hard no-match → skip candidate
 * 4. If no deterministic decision → return "no_match" (heuristic scoring added in Step 7)
 */
export async function runMatchEngine(
	input: MatchEngineInput,
): Promise<MatchEngineResult> {
	const { sourceRecordPk, sourceSystemId, identifiers, attributes } = input;

	// Map identifier types for person creation
	const typedIdentifiers = identifiers.map((id) => ({
		type: id.type as IdentifierType,
		rawValue: id.rawValue,
		normalizedValue: id.normalizedValue,
		hashedValue: id.hashedValue,
		isVerified: id.isVerified,
		qualityFlag: id.qualityFlag,
	}));

	// 1. Generate candidates
	const candidates = await generateCandidates(
		identifiers,
		input.existingPersonId ? [input.existingPersonId] : [],
	);

	// 2. No candidates → create new person
	if (candidates.length === 0) {
		const person = await createPersonFromSourceRecord(
			sourceRecordPk,
			sourceSystemId,
			typedIdentifiers,
			attributes,
			"match_engine",
		);
		return { personId: person.personId, action: "created" };
	}

	// 3. Evaluate each candidate
	for (const candidate of candidates) {
		// 3a. Check no-match lock
		if (input.existingPersonId) {
			const lockResult = await checkNoMatchLock(
				input.existingPersonId,
				candidate.personId,
			);
			if (lockResult?.decision === "no_match") continue;
		}

		// 3b. Evaluate deterministic rules
		const deterministicResult = await evaluateDeterministicRules(
			identifiers,
			candidate.personId,
		);

		if (deterministicResult.decision === "no_match") {
			// Record the no-match decision
			await prisma.matchDecision.create({
				data: {
					leftEntityType: "source_record",
					leftEntityId: sourceRecordPk,
					rightEntityType: "person",
					rightEntityId: candidate.personId,
					engineType: "deterministic",
					engineVersion: "v1.0.0",
					decision: "no_match",
					confidence: 0,
					reasons: deterministicResult.reasons as unknown as Prisma.InputJsonValue,
					blockingConflicts: deterministicResult.blockingConflicts as unknown as Prisma.InputJsonValue,
					policyVersion: "v1.0.0",
				},
			});
			continue;
		}

		if (deterministicResult.decision === "merge") {
			// Record the merge decision
			const matchDecision = await prisma.matchDecision.create({
				data: {
					leftEntityType: "source_record",
					leftEntityId: sourceRecordPk,
					rightEntityType: "person",
					rightEntityId: candidate.personId,
					engineType: "deterministic",
					engineVersion: "v1.0.0",
					decision: "merge",
					confidence: 1.0,
					reasons: deterministicResult.reasons as unknown as Prisma.InputJsonValue,
					blockingConflicts: [] as unknown as Prisma.InputJsonValue,
					policyVersion: "v1.0.0",
				},
			});

			// Create a temporary person for the source record, then merge
			const tempPerson = await createPersonFromSourceRecord(
				sourceRecordPk,
				sourceSystemId,
				typedIdentifiers,
				attributes,
				"match_engine",
			);

			const mergeEvent = await mergePersons(
				tempPerson.personId,
				candidate.personId,
				matchDecision.matchDecisionId,
				"system",
				"match_engine",
				deterministicResult.reasons.join("; "),
			);

			return {
				personId: candidate.personId,
				action: "merged",
				matchDecisionId: matchDecision.matchDecisionId,
				mergeEventId: mergeEvent.mergeEventId,
			};
		}

		// No deterministic decision for this candidate — will go to heuristic in Step 7
		// For now, record as review-band placeholder
	}

	// 4. No deterministic match found — create new person for now
	// In Step 7, this will be replaced with heuristic scoring that may produce
	// merge/review/no_match decisions instead of always creating a new person.
	const person = await createPersonFromSourceRecord(
		sourceRecordPk,
		sourceSystemId,
		typedIdentifiers,
		attributes,
		"match_engine",
	);
	return { personId: person.personId, action: "no_match" };
}
