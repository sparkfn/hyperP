import { createHash } from "node:crypto";
import { prisma } from "../../common/db.js";
import { NotFoundError } from "../../common/errors.js";
import type { IdentifierType, TrustTier, Prisma } from "../../generated/prisma/client.js";
import { normalizeAttributes, normalizeIdentifier } from "./normalizer.js";

// ─── Types ──────────────────────────────────────────────────────────

export interface IngestPayload {
	source_record_id: string;
	source_record_version?: string;
	observed_at?: string;
	identifiers: Array<{
		type: string;
		value: string;
		is_verified?: boolean;
	}>;
	attributes: Record<string, string>;
	raw_payload: Record<string, unknown>;
}

export interface IngestResult {
	source_record_pk: string;
	source_system_id: string;
	source_record_id: string;
	status: "created" | "duplicate";
	normalized_identifier_count: number;
	normalized_attribute_count: number;
	rejection_reasons: string[];
}

// ─── Helpers ────────────────────────────────────────────────────────

function computeRecordHash(payload: Record<string, unknown>): string {
	const sorted = JSON.stringify(payload, Object.keys(payload).sort());
	return createHash("sha256").update(sorted).digest("hex");
}

function mapIdentifierType(type: string): IdentifierType | null {
	const mapping: Record<string, IdentifierType> = {
		phone: "phone",
		email: "email",
		government_id_hash: "government_id_hash",
		external_customer_id: "external_customer_id",
		membership_id: "membership_id",
		crm_contact_id: "crm_contact_id",
		loyalty_id: "loyalty_id",
		custom: "custom",
	};
	return mapping[type] ?? null;
}

// ─── Ingest Run ─────────────────────────────────────────────────────

export async function createIngestRun(
	sourceSystemId: string,
	runType: string,
) {
	return prisma.ingestRun.create({
		data: {
			sourceSystemId,
			runType,
			status: "running",
		},
	});
}

export async function completeIngestRun(
	ingestRunId: string,
	status: "completed" | "failed",
) {
	return prisma.ingestRun.update({
		where: { ingestRunId },
		data: { status, finishedAt: new Date() },
	});
}

// ─── Core Ingestion ─────────────────────────────────────────────────

export async function ingestRecord(
	sourceKey: string,
	payload: IngestPayload,
	ingestRunId?: string,
): Promise<IngestResult> {
	// Resolve source system
	const sourceSystem = await prisma.sourceSystem.findUnique({
		where: { sourceKey },
		include: { fieldTrusts: true },
	});
	if (!sourceSystem) {
		throw new NotFoundError("SourceSystem", sourceKey);
	}

	const sourceSystemId = sourceSystem.sourceSystemId;
	const recordHash = computeRecordHash(payload.raw_payload);
	const rejectionReasons: string[] = [];

	// Check idempotency — if same source_system + source_record_id + hash exists, skip
	const existing = await prisma.sourceRecord.findUnique({
		where: {
			sourceSystemId_sourceRecordId_recordHash: {
				sourceSystemId,
				sourceRecordId: payload.source_record_id,
				recordHash,
			},
		},
	});

	if (existing) {
		return {
			source_record_pk: existing.sourceRecordPk,
			source_system_id: sourceSystemId,
			source_record_id: payload.source_record_id,
			status: "duplicate",
			normalized_identifier_count: 0,
			normalized_attribute_count: 0,
			rejection_reasons: [],
		};
	}

	// Normalize identifiers
	const normalizedIdentifiers = payload.identifiers.map((id) =>
		normalizeIdentifier(id.type, id.value, id.is_verified ?? false),
	);

	// Normalize attributes
	const normalizedAttrs = normalizeAttributes(payload.attributes);

	// Build normalized payload for storage
	const normalizedPayload = {
		identifiers: normalizedIdentifiers.map((id) => ({
			type: id.type,
			raw_value: id.rawValue,
			normalized_value: id.normalizedValue,
			quality_flag: id.qualityFlag,
		})),
		attributes: Object.fromEntries(
			Object.entries(normalizedAttrs).map(([k, v]) => [
				k,
				{ normalized_value: v.normalizedValue, quality_flag: v.qualityFlag },
			]),
		),
	};

	// Build field trust lookup
	const trustByField = new Map<string, TrustTier>();
	for (const ft of sourceSystem.fieldTrusts) {
		trustByField.set(ft.fieldName, ft.trustTier);
	}

	// Persist everything in a transaction
	const result = await prisma.$transaction(async (tx) => {
		// 1. Create source record
		const sourceRecord = await tx.sourceRecord.create({
			data: {
				sourceSystemId,
				sourceRecordId: payload.source_record_id,
				sourceRecordVersion: payload.source_record_version,
				ingestRunId: ingestRunId ?? null,
				recordHash,
				rawPayload: payload.raw_payload as Prisma.InputJsonValue,
				normalizedPayload: normalizedPayload as unknown as Prisma.InputJsonValue,
				observedAt: payload.observed_at ? new Date(payload.observed_at) : null,
				linkStatus: "pending_review",
			},
		});

		// 2. Create person identifiers (unlinked — person linkage happens in matching)
		// For now, we store them without a person_id — they'll be linked after matching
		// Actually per schema, person_id is required on person_identifier.
		// At ingestion time, we don't have a person yet. We'll store normalized data
		// in the source_record.normalized_payload and create person_identifiers during
		// the matching/linking phase. Track valid identifier count for the response.
		let identifierCount = 0;
		for (const id of normalizedIdentifiers) {
			const idType = mapIdentifierType(id.type);
			if (!idType) {
				rejectionReasons.push(`Unknown identifier type: ${id.type}`);
				continue;
			}
			if (id.normalizedValue || id.hashedValue) {
				identifierCount++;
			} else {
				rejectionReasons.push(
					`Identifier ${id.type}='${id.rawValue}' failed normalization: ${id.qualityFlag}`,
				);
			}
		}

		// 3. Track attribute count
		let attributeCount = 0;
		for (const [key, attr] of Object.entries(normalizedAttrs)) {
			if (attr.qualityFlag === "valid") {
				attributeCount++;
			} else {
				rejectionReasons.push(
					`Attribute ${key}='${attr.rawValue}' flagged: ${attr.qualityFlag}`,
				);
			}
		}

		// 4. If everything failed normalization, create a rejection record
		if (identifierCount === 0 && attributeCount === 0) {
			await tx.sourceRecordRejection.create({
				data: {
					sourceSystemId,
					sourceRecordId: payload.source_record_id,
					ingestRunId: ingestRunId ?? null,
					rejectionReason: `All identifiers and attributes failed normalization: ${rejectionReasons.join("; ")}`,
					rawPayload: payload.raw_payload as Prisma.InputJsonValue,
				},
			});
		}

		return {
			source_record_pk: sourceRecord.sourceRecordPk,
			source_system_id: sourceSystemId,
			source_record_id: payload.source_record_id,
			status: "created" as const,
			normalized_identifier_count: identifierCount,
			normalized_attribute_count: attributeCount,
			rejection_reasons: rejectionReasons,
		};
	});

	return result;
}

/**
 * Ingest a batch of records, tracking results per record.
 */
export async function ingestBatch(
	sourceKey: string,
	records: IngestPayload[],
): Promise<{ ingest_run_id: string; results: IngestResult[] }> {
	const sourceSystem = await prisma.sourceSystem.findUnique({
		where: { sourceKey },
	});
	if (!sourceSystem) {
		throw new NotFoundError("SourceSystem", sourceKey);
	}

	const run = await createIngestRun(sourceSystem.sourceSystemId, "batch");
	const results: IngestResult[] = [];

	try {
		for (const record of records) {
			try {
				const result = await ingestRecord(sourceKey, record, run.ingestRunId);
				results.push(result);
			} catch (err) {
				// Record-level failure — capture rejection, continue batch
				await prisma.sourceRecordRejection.create({
					data: {
						sourceSystemId: sourceSystem.sourceSystemId,
						sourceRecordId: record.source_record_id,
						ingestRunId: run.ingestRunId,
						rejectionReason: err instanceof Error ? err.message : String(err),
						rawPayload: record.raw_payload as Prisma.InputJsonValue,
					},
				});
				results.push({
					source_record_pk: "",
					source_system_id: sourceSystem.sourceSystemId,
					source_record_id: record.source_record_id,
					status: "created",
					normalized_identifier_count: 0,
					normalized_attribute_count: 0,
					rejection_reasons: [err instanceof Error ? err.message : String(err)],
				});
			}
		}
		await completeIngestRun(run.ingestRunId, "completed");
	} catch (err) {
		await completeIngestRun(run.ingestRunId, "failed");
		throw err;
	}

	return { ingest_run_id: run.ingestRunId, results };
}
