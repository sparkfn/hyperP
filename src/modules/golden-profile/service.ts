import { prisma } from "../../common/db.js";
import type { Prisma } from "../../generated/prisma/client.js";

const COMPUTATION_VERSION = "v1.0.0";

// Trust tier priority (lower = more trusted)
const TRUST_PRIORITY: Record<string, number> = {
	tier_1: 1,
	tier_2: 2,
	tier_3: 3,
	tier_4: 4,
};

/**
 * Compute and persist the golden profile for a person.
 *
 * Survivorship rules:
 * - Verified beats unverified
 * - Higher trust tier beats lower
 * - Newer beats older
 * - Non-placeholder beats placeholder
 *
 * Called synchronously within merge transactions.
 */
export async function computeGoldenProfile(personId: string) {
	// Load all attribute facts for this person
	const facts = await prisma.personAttributeFact.findMany({
		where: { personId, qualityFlag: "valid" },
		orderBy: [{ sourceTrustTier: "asc" }, { observedAt: "desc" }],
	});

	// Load all active identifiers
	const identifiers = await prisma.personIdentifier.findMany({
		where: { personId, isActive: true, qualityFlag: "valid" },
		orderBy: [{ isVerified: "desc" }, { lastSeenAt: "desc" }],
	});

	// Load manual overrides
	const overrides = await prisma.survivorshipOverride.findMany({
		where: { personId },
	});
	const overrideByField = new Map(
		overrides.map((o) => [o.attributeName, o.selectedPersonAttributeFactId]),
	);

	// Resolve preferred values
	const preferredFullName = resolveAttribute(facts, "full_name", overrideByField);
	const preferredDob = resolveAttribute(facts, "dob", overrideByField);
	const preferredAddress = resolveAttribute(facts, "address", overrideByField);
	const preferredPhone = resolveIdentifier(identifiers, "phone");
	const preferredEmail = resolveIdentifier(identifiers, "email");

	// Calculate completeness
	const fields = [preferredFullName, preferredPhone, preferredEmail, preferredDob, preferredAddress];
	const filledCount = fields.filter((f) => f.value !== null).length;
	const completenessScore = filledCount / fields.length;

	// Upsert golden profile
	await prisma.goldenProfile.upsert({
		where: { personId },
		update: {
			preferredFullName: preferredFullName.value as string | null,
			preferredPhone: preferredPhone.value as string | null,
			preferredEmail: preferredEmail.value as string | null,
			preferredDob: preferredDob.value ? new Date(preferredDob.value as string) : null,
			preferredAddress: preferredAddress.value
				? (preferredAddress.value as Prisma.InputJsonValue)
				: undefined,
			profileCompletenessScore: completenessScore,
			computedAt: new Date(),
			computationVersion: COMPUTATION_VERSION,
		},
		create: {
			personId,
			preferredFullName: preferredFullName.value as string | null,
			preferredPhone: preferredPhone.value as string | null,
			preferredEmail: preferredEmail.value as string | null,
			preferredDob: preferredDob.value ? new Date(preferredDob.value as string) : null,
			preferredAddress: preferredAddress.value
				? (preferredAddress.value as Prisma.InputJsonValue)
				: undefined,
			profileCompletenessScore: completenessScore,
			computationVersion: COMPUTATION_VERSION,
		},
	});

	// Update lineage
	const lineageEntries = [
		{ fieldName: "preferred_full_name", factId: preferredFullName.factId, identifierId: null },
		{ fieldName: "preferred_phone", factId: null, identifierId: preferredPhone.identifierId },
		{ fieldName: "preferred_email", factId: null, identifierId: preferredEmail.identifierId },
		{ fieldName: "preferred_dob", factId: preferredDob.factId, identifierId: null },
		{ fieldName: "preferred_address", factId: preferredAddress.factId, identifierId: null },
	];

	for (const entry of lineageEntries) {
		await prisma.goldenProfileLineage.upsert({
			where: {
				personId_fieldName: { personId, fieldName: entry.fieldName },
			},
			update: {
				personAttributeFactId: entry.factId,
				personIdentifierId: entry.identifierId,
			},
			create: {
				personId,
				fieldName: entry.fieldName,
				personAttributeFactId: entry.factId,
				personIdentifierId: entry.identifierId,
			},
		});
	}

	return { personId, completenessScore };
}

// ─── Resolution Helpers ─────────────────────────────────────────────

interface ResolvedValue {
	value: unknown;
	factId: string | null;
	identifierId?: string | null;
}

function resolveAttribute(
	facts: Array<{
		personAttributeFactId: string;
		attributeName: string;
		attributeValue: unknown;
		sourceTrustTier: string;
		observedAt: Date;
	}>,
	fieldName: string,
	overrides: Map<string, string>,
): ResolvedValue {
	// Check for manual override first
	const overrideFactId = overrides.get(fieldName);
	if (overrideFactId) {
		const overrideFact = facts.find(
			(f) => f.personAttributeFactId === overrideFactId,
		);
		if (overrideFact) {
			return { value: overrideFact.attributeValue, factId: overrideFact.personAttributeFactId };
		}
	}

	// Find best fact by survivorship rules:
	// Facts are already ordered by trust tier asc, observed_at desc
	const matching = facts.filter((f) => f.attributeName === fieldName);
	if (matching.length === 0) return { value: null, factId: null };

	// Best = highest trust (lowest tier number), then most recent
	const best = matching[0];
	return { value: best.attributeValue, factId: best.personAttributeFactId };
}

function resolveIdentifier(
	identifiers: Array<{
		personIdentifierId: string;
		identifierType: string;
		normalizedValue: string | null;
		isVerified: boolean;
		lastSeenAt: Date;
	}>,
	type: string,
): ResolvedValue & { identifierId: string | null } {
	// Identifiers are already ordered by isVerified desc, lastSeenAt desc
	const matching = identifiers.filter(
		(id) => id.identifierType === type && id.normalizedValue,
	);
	if (matching.length === 0) {
		return { value: null, factId: null, identifierId: null };
	}

	const best = matching[0];
	return {
		value: best.normalizedValue,
		factId: null,
		identifierId: best.personIdentifierId,
	};
}
