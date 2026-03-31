import { prisma } from "../../common/db.js";
import { NotFoundError } from "../../common/errors.js";
import type { IdentifierType, TrustTier, Prisma } from "../../generated/prisma/client.js";
import { appendLineage } from "./merge-lineage.js";

// ─── Person Lookup ──────────────────────────────────────────────────

export async function getActivePerson(personId: string) {
	const person = await prisma.person.findUnique({
		where: { personId },
		include: {
			goldenProfile: true,
			identifiers: { where: { isActive: true } },
			sourceRecords: { orderBy: { ingestedAt: "desc" } },
		},
	});
	if (!person) throw new NotFoundError("Person", personId);

	// Follow merge chain (max 1 hop due to path compression)
	if (person.status === "merged" && person.mergedIntoPersonId) {
		return getActivePerson(person.mergedIntoPersonId);
	}

	return person;
}

export async function searchPersonsByIdentifier(
	identifierType: IdentifierType,
	normalizedValue: string,
) {
	const identifiers = await prisma.personIdentifier.findMany({
		where: {
			identifierType,
			normalizedValue,
			isActive: true,
			person: { status: "active" },
		},
		include: {
			person: {
				include: { goldenProfile: true },
			},
		},
	});

	// Deduplicate by person
	const seen = new Set<string>();
	return identifiers
		.filter((id) => {
			if (seen.has(id.personId)) return false;
			seen.add(id.personId);
			return true;
		})
		.map((id) => id.person);
}

// ─── Person Creation ────────────────────────────────────────────────

export async function createPersonFromSourceRecord(
	sourceRecordPk: string,
	sourceSystemId: string,
	identifiers: Array<{
		type: IdentifierType;
		rawValue: string | null;
		normalizedValue: string | null;
		hashedValue: string | null;
		isVerified: boolean;
		qualityFlag: "valid" | "invalid_format" | "placeholder_value";
	}>,
	attributes: Array<{
		attributeName: string;
		attributeValue: unknown;
		trustTier: TrustTier;
		observedAt: Date;
	}>,
	actorId: string,
) {
	return prisma.$transaction(async (tx) => {
		// 1. Create person
		const person = await tx.person.create({
			data: {
				primarySourceSystemId: sourceSystemId,
			},
		});

		// 2. Link source record
		await tx.sourceRecord.update({
			where: { sourceRecordPk },
			data: {
				linkedPersonId: person.personId,
				linkStatus: "linked",
			},
		});

		// 3. Create merge event (person_created)
		await tx.mergeEvent.create({
			data: {
				eventType: "person_created",
				toPersonId: person.personId,
				actorType: "system",
				actorId,
				reason: "New person created — no matching candidates found",
			},
		});

		// 4. Create person identifiers
		for (const id of identifiers) {
			if (!id.normalizedValue && !id.hashedValue) continue;
			await tx.personIdentifier.create({
				data: {
					personId: person.personId,
					sourceRecordPk,
					sourceSystemId,
					identifierType: id.type,
					rawValue: id.rawValue,
					normalizedValue: id.normalizedValue,
					hashedValue: id.hashedValue,
					isVerified: id.isVerified,
					qualityFlag: id.qualityFlag,
				},
			});
		}

		// 5. Create person attribute facts
		for (const attr of attributes) {
			await tx.personAttributeFact.create({
				data: {
					personId: person.personId,
					sourceRecordPk,
					sourceSystemId,
					attributeName: attr.attributeName,
					attributeValue: attr.attributeValue as Prisma.InputJsonValue,
					sourceTrustTier: attr.trustTier,
					observedAt: attr.observedAt,
				},
			});
		}

		return person;
	});
}

// ─── Person Merge ───────────────────────────────────────────────────

export async function mergePersons(
	fromPersonId: string,
	toPersonId: string,
	matchDecisionId: string | null,
	actorType: "system" | "reviewer" | "admin",
	actorId: string,
	reason: string,
) {
	return prisma.$transaction(async (tx) => {
		const fromPerson = await tx.person.findUnique({ where: { personId: fromPersonId } });
		const toPerson = await tx.person.findUnique({ where: { personId: toPersonId } });

		if (!fromPerson) throw new NotFoundError("Person", fromPersonId);
		if (!toPerson) throw new NotFoundError("Person", toPersonId);

		// 1. Create merge event
		const eventType = actorType === "system" ? "auto_merge" : "manual_merge";
		const mergeEvent = await tx.mergeEvent.create({
			data: {
				eventType,
				fromPersonId,
				toPersonId,
				matchDecisionId,
				actorType,
				actorId,
				reason,
			},
		});

		// 2. Track affected source records
		const affectedRecords = await tx.sourceRecord.findMany({
			where: { linkedPersonId: fromPersonId },
		});
		for (const sr of affectedRecords) {
			await tx.mergeEventSourceRecord.create({
				data: {
					mergeEventId: mergeEvent.mergeEventId,
					sourceRecordPk: sr.sourceRecordPk,
				},
			});
		}

		// 3. Relink source records
		await tx.sourceRecord.updateMany({
			where: { linkedPersonId: fromPersonId },
			data: { linkedPersonId: toPersonId },
		});

		// 4. Relink identifiers
		await tx.personIdentifier.updateMany({
			where: { personId: fromPersonId },
			data: { personId: toPersonId },
		});

		// 5. Relink attribute facts
		await tx.personAttributeFact.updateMany({
			where: { personId: fromPersonId },
			data: { personId: toPersonId },
		});

		// 6. Path compression: anything merged into fromPerson now points to toPerson
		await tx.person.updateMany({
			where: { mergedIntoPersonId: fromPersonId },
			data: { mergedIntoPersonId: toPersonId },
		});

		// 7. Mark from person as merged
		await tx.person.update({
			where: { personId: fromPersonId },
			data: {
				status: "merged",
				mergedIntoPersonId: toPersonId,
			},
		});

		// 8. Update merge lineage on surviving person
		const updatedLineage = appendLineage(toPerson.mergeLineage, {
			absorbedPersonId: fromPersonId,
			mergeEventId: mergeEvent.mergeEventId,
			actor: `${actorType}:${actorId}`,
			timestamp: new Date().toISOString(),
		});
		await tx.person.update({
			where: { personId: toPersonId },
			data: { mergeLineage: updatedLineage },
		});

		return mergeEvent;
	});
}

// ─── Person Audit ───────────────────────────────────────────────────

export async function getPersonAudit(personId: string) {
	return prisma.mergeEvent.findMany({
		where: {
			OR: [{ fromPersonId: personId }, { toPersonId: personId }],
		},
		orderBy: { createdAt: "desc" },
		include: { sourceRecords: true },
	});
}
