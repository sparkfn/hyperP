import { prisma } from "../../common/db.js";
import type { NormalizedIdentifier } from "../ingestion/normalizer.js";
import { hasNoMatchLock } from "./candidate-generation.js";

export interface DeterministicResult {
	decision: "merge" | "no_match" | null;
	reasons: string[];
	blockingConflicts: string[];
}

/**
 * Evaluate deterministic hard rules for a candidate pair.
 *
 * Hard merge:
 *   - Exact verified government ID match from trusted sources
 *   - Trusted migration-map match
 *
 * Hard no-match:
 *   - Conflicting government identifiers
 *   - Manual no-match lock exists
 */
export async function evaluateDeterministicRules(
	incomingIdentifiers: NormalizedIdentifier[],
	candidatePersonId: string,
): Promise<DeterministicResult> {
	const reasons: string[] = [];
	const blockingConflicts: string[] = [];

	// Load candidate person's active identifiers
	const existingIdentifiers = await prisma.personIdentifier.findMany({
		where: {
			personId: candidatePersonId,
			isActive: true,
		},
	});

	// Check government ID rules
	const incomingGovIds = incomingIdentifiers.filter(
		(id) => id.type === "government_id_hash" && (id.normalizedValue || id.hashedValue),
	);
	const existingGovIds = existingIdentifiers.filter(
		(id) => id.identifierType === "government_id_hash",
	);

	if (incomingGovIds.length > 0 && existingGovIds.length > 0) {
		for (const incoming of incomingGovIds) {
			const incomingHash = incoming.hashedValue ?? incoming.normalizedValue;
			for (const existing of existingGovIds) {
				const existingHash = existing.hashedValue ?? existing.normalizedValue;

				if (incomingHash && existingHash) {
					if (incomingHash === existingHash) {
						// Government ID match — hard merge if both verified
						if (incoming.isVerified && existing.isVerified) {
							reasons.push("Exact verified government ID match");
							return { decision: "merge", reasons, blockingConflicts };
						}
						reasons.push("Government ID match (unverified — not a hard merge)");
					} else {
						// Government ID conflict — hard no-match
						blockingConflicts.push("Conflicting government identifiers");
						return { decision: "no_match", reasons, blockingConflicts };
					}
				}
			}
		}
	}

	// Check trusted migration-map IDs (external_customer_id from tier_1 sources)
	const incomingExtIds = incomingIdentifiers.filter(
		(id) =>
			(id.type === "external_customer_id" || id.type === "crm_contact_id") &&
			id.normalizedValue &&
			id.qualityFlag === "valid",
	);
	const existingExtIds = existingIdentifiers.filter(
		(id) =>
			(id.identifierType === "external_customer_id" ||
				id.identifierType === "crm_contact_id") &&
			id.isVerified,
	);

	for (const incoming of incomingExtIds) {
		for (const existing of existingExtIds) {
			if (
				incoming.type === existing.identifierType &&
				incoming.normalizedValue === existing.normalizedValue
			) {
				reasons.push(`Trusted migration-map match: ${incoming.type}=${incoming.normalizedValue}`);
				return { decision: "merge", reasons, blockingConflicts };
			}
		}
	}

	// No deterministic decision — pass to heuristic layer
	return { decision: null, reasons, blockingConflicts };
}

/**
 * Check for manual no-match lock and return a hard no-match if one exists.
 */
export async function checkNoMatchLock(
	sourcePersonId: string | null,
	candidatePersonId: string,
): Promise<DeterministicResult | null> {
	if (!sourcePersonId) return null;

	const locked = await hasNoMatchLock(sourcePersonId, candidatePersonId);
	if (locked) {
		return {
			decision: "no_match",
			reasons: [],
			blockingConflicts: ["Manual no-match lock exists"],
		};
	}
	return null;
}
