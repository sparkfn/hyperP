import "dotenv/config";
import { PrismaClient } from "../src/generated/prisma/client.js";

const prisma = new PrismaClient();

async function seed() {
	console.log("Seeding source systems...");

	const systems = [
		{ sourceKey: "bitrix", displayName: "Bitrix CRM", systemType: "crm" },
		{ sourceKey: "pos", displayName: "Point of Sale", systemType: "pos" },
	];

	for (const system of systems) {
		await prisma.sourceSystem.upsert({
			where: { sourceKey: system.sourceKey },
			update: {},
			create: system,
		});
		console.log(`  - ${system.sourceKey}`);
	}

	// Default field trust settings
	const bitrix = await prisma.sourceSystem.findUnique({
		where: { sourceKey: "bitrix" },
	});
	const pos = await prisma.sourceSystem.findUnique({
		where: { sourceKey: "pos" },
	});

	if (bitrix) {
		const bitrixTrusts = [
			{ fieldName: "full_name", trustTier: "tier_1" as const },
			{ fieldName: "email", trustTier: "tier_1" as const },
			{ fieldName: "phone", trustTier: "tier_2" as const },
			{ fieldName: "address", trustTier: "tier_2" as const },
		];
		for (const t of bitrixTrusts) {
			await prisma.sourceFieldTrust.upsert({
				where: {
					sourceSystemId_fieldName: {
						sourceSystemId: bitrix.sourceSystemId,
						fieldName: t.fieldName,
					},
				},
				update: { trustTier: t.trustTier },
				create: {
					sourceSystemId: bitrix.sourceSystemId,
					fieldName: t.fieldName,
					trustTier: t.trustTier,
				},
			});
		}
		console.log("  - bitrix field trusts set");
	}

	if (pos) {
		const posTrusts = [
			{ fieldName: "full_name", trustTier: "tier_2" as const },
			{ fieldName: "phone", trustTier: "tier_1" as const },
			{ fieldName: "email", trustTier: "tier_3" as const },
		];
		for (const t of posTrusts) {
			await prisma.sourceFieldTrust.upsert({
				where: {
					sourceSystemId_fieldName: {
						sourceSystemId: pos.sourceSystemId,
						fieldName: t.fieldName,
					},
				},
				update: { trustTier: t.trustTier },
				create: {
					sourceSystemId: pos.sourceSystemId,
					fieldName: t.fieldName,
					trustTier: t.trustTier,
				},
			});
		}
		console.log("  - pos field trusts set");
	}

	console.log("Seeding complete.");
}

seed()
	.catch((e) => {
		console.error(e);
		process.exit(1);
	})
	.finally(() => prisma.$disconnect());
