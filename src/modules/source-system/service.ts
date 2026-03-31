import { prisma } from "../../common/db.js";
import { ConflictError, NotFoundError } from "../../common/errors.js";
import type { TrustTier } from "../../generated/prisma/client.js";

export interface CreateSourceSystemInput {
	sourceKey: string;
	displayName: string;
	systemType: string;
}

export interface UpdateSourceSystemInput {
	displayName?: string;
	systemType?: string;
	isActive?: boolean;
}

export interface SetFieldTrustInput {
	fieldName: string;
	trustTier: TrustTier;
	notes?: string;
}

export async function listSourceSystems() {
	return prisma.sourceSystem.findMany({
		orderBy: { createdAt: "asc" },
		include: { fieldTrusts: true },
	});
}

export async function getSourceSystem(sourceSystemId: string) {
	const system = await prisma.sourceSystem.findUnique({
		where: { sourceSystemId },
		include: { fieldTrusts: true },
	});
	if (!system) throw new NotFoundError("SourceSystem", sourceSystemId);
	return system;
}

export async function createSourceSystem(input: CreateSourceSystemInput) {
	const existing = await prisma.sourceSystem.findUnique({
		where: { sourceKey: input.sourceKey },
	});
	if (existing) {
		throw new ConflictError(`Source system with key '${input.sourceKey}' already exists`);
	}
	return prisma.sourceSystem.create({ data: input });
}

export async function updateSourceSystem(
	sourceSystemId: string,
	input: UpdateSourceSystemInput,
) {
	await getSourceSystem(sourceSystemId); // throws if not found
	return prisma.sourceSystem.update({
		where: { sourceSystemId },
		data: input,
	});
}

export async function setFieldTrust(
	sourceSystemId: string,
	input: SetFieldTrustInput,
) {
	await getSourceSystem(sourceSystemId); // throws if not found
	return prisma.sourceFieldTrust.upsert({
		where: {
			sourceSystemId_fieldName: {
				sourceSystemId,
				fieldName: input.fieldName,
			},
		},
		update: {
			trustTier: input.trustTier,
			notes: input.notes,
		},
		create: {
			sourceSystemId,
			fieldName: input.fieldName,
			trustTier: input.trustTier,
			notes: input.notes,
		},
	});
}

export async function listFieldTrusts(sourceSystemId: string) {
	await getSourceSystem(sourceSystemId);
	return prisma.sourceFieldTrust.findMany({
		where: { sourceSystemId },
		orderBy: { fieldName: "asc" },
	});
}
