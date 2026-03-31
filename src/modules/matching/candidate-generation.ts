import { prisma } from "../../common/db.js";
import type { IdentifierType } from "../../generated/prisma/client.js";
import type { NormalizedIdentifier } from "../ingestion/normalizer.js";

export interface CandidateMatch {
	personId: string;
	blockingReason: string;
	identifierType: IdentifierType;
	matchedValue: string;
}

// Configurable cardinality caps per identifier type.
// If a blocking key matches more persons than this, skip it.
const DEFAULT_CARDINALITY_CAPS: Record<string, number> = {
	phone: 10,
	email: 10,
	government_id_hash: 3,
	external_customer_id: 5,
	membership_id: 5,
	crm_contact_id: 5,
	loyalty_id: 5,
	custom: 10,
};

/**
 * Generate candidate persons that might match a set of normalized identifiers.
 * Uses blocking keys: exact match on normalized identifier values.
 * Skips invalid/placeholder identifiers and applies cardinality caps.
 */
export async function generateCandidates(
	identifiers: NormalizedIdentifier[],
	excludePersonIds: string[] = [],
): Promise<CandidateMatch[]> {
	const candidates: CandidateMatch[] = [];
	const seenPersonIds = new Set<string>(excludePersonIds);

	for (const id of identifiers) {
		// Skip identifiers that failed normalization
		if (!id.normalizedValue && !id.hashedValue) continue;
		if (id.qualityFlag !== "valid") continue;

		const identifierType = id.type as IdentifierType;
		const lookupValue = id.normalizedValue ?? id.hashedValue;
		if (!lookupValue) continue;

		// Find active identifiers with the same normalized value
		const matchField = id.normalizedValue ? "normalizedValue" : "hashedValue";
		const matches = await prisma.personIdentifier.findMany({
			where: {
				identifierType,
				[matchField]: lookupValue,
				isActive: true,
				person: { status: "active" },
			},
			select: {
				personId: true,
				identifierType: true,
				normalizedValue: true,
			},
		});

		// Cardinality cap check
		const cap = DEFAULT_CARDINALITY_CAPS[id.type] ?? 10;
		const uniquePersonIds = new Set(matches.map((m) => m.personId));
		if (uniquePersonIds.size > cap) {
			// Log skipped blocking key (would use structured logger in production)
			console.warn(
				`Cardinality cap exceeded for ${id.type}=${lookupValue}: ` +
				`${uniquePersonIds.size} persons (cap: ${cap}). Skipping.`,
			);
			continue;
		}

		// Check for manual no-match locks
		for (const match of matches) {
			if (seenPersonIds.has(match.personId)) continue;

			candidates.push({
				personId: match.personId,
				blockingReason: `exact_${id.type}_match`,
				identifierType,
				matchedValue: lookupValue,
			});
			seenPersonIds.add(match.personId);
		}
	}

	return candidates;
}

/**
 * Check if a no-match lock exists between two persons.
 */
export async function hasNoMatchLock(
	personIdA: string,
	personIdB: string,
): Promise<boolean> {
	const [leftPersonId, rightPersonId] =
		personIdA < personIdB ? [personIdA, personIdB] : [personIdB, personIdA];

	const lock = await prisma.personPairLock.findFirst({
		where: {
			leftPersonId,
			rightPersonId,
			lockType: "manual_no_match",
			OR: [
				{ expiresAt: null },
				{ expiresAt: { gt: new Date() } },
			],
		},
	});

	return lock !== null;
}
