import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock prisma before importing the module
vi.mock("../../src/common/db.js", () => ({
	prisma: {
		personIdentifier: {
			findMany: vi.fn(),
		},
		personPairLock: {
			findFirst: vi.fn(),
		},
	},
}));

import { prisma } from "../../src/common/db.js";
import { evaluateDeterministicRules } from "../../src/modules/matching/deterministic.js";
import type { NormalizedIdentifier } from "../../src/modules/ingestion/normalizer.js";

const mockFindMany = vi.mocked(prisma.personIdentifier.findMany);

beforeEach(() => {
	vi.clearAllMocks();
});

describe("evaluateDeterministicRules", () => {
	it("returns hard merge for exact verified government ID match", async () => {
		const incoming: NormalizedIdentifier[] = [
			{
				type: "government_id_hash",
				rawValue: "S1234567A",
				normalizedValue: null,
				hashedValue: "hash_abc",
				isVerified: true,
				qualityFlag: "valid",
			},
		];

		mockFindMany.mockResolvedValue([
			{
				personIdentifierId: "pid-1",
				personId: "person-1",
				sourceRecordPk: null,
				sourceSystemId: "ss-1",
				identifierType: "government_id_hash",
				rawValue: null,
				normalizedValue: null,
				hashedValue: "hash_abc",
				isVerified: true,
				verificationMethod: null,
				isActive: true,
				qualityFlag: "valid",
				firstSeenAt: new Date(),
				lastSeenAt: new Date(),
				lastConfirmedAt: new Date(),
				metadata: {},
			},
		] as any);

		const result = await evaluateDeterministicRules(incoming, "person-1");
		expect(result.decision).toBe("merge");
		expect(result.reasons).toContain("Exact verified government ID match");
	});

	it("returns hard no-match for conflicting government IDs", async () => {
		const incoming: NormalizedIdentifier[] = [
			{
				type: "government_id_hash",
				rawValue: "S1234567A",
				normalizedValue: null,
				hashedValue: "hash_abc",
				isVerified: true,
				qualityFlag: "valid",
			},
		];

		mockFindMany.mockResolvedValue([
			{
				personIdentifierId: "pid-1",
				personId: "person-1",
				sourceRecordPk: null,
				sourceSystemId: "ss-1",
				identifierType: "government_id_hash",
				rawValue: null,
				normalizedValue: null,
				hashedValue: "hash_xyz",
				isVerified: true,
				verificationMethod: null,
				isActive: true,
				qualityFlag: "valid",
				firstSeenAt: new Date(),
				lastSeenAt: new Date(),
				lastConfirmedAt: new Date(),
				metadata: {},
			},
		] as any);

		const result = await evaluateDeterministicRules(incoming, "person-1");
		expect(result.decision).toBe("no_match");
		expect(result.blockingConflicts).toContain("Conflicting government identifiers");
	});

	it("returns null when no hard rules apply", async () => {
		const incoming: NormalizedIdentifier[] = [
			{
				type: "phone",
				rawValue: "+6591234567",
				normalizedValue: "+6591234567",
				hashedValue: null,
				isVerified: false,
				qualityFlag: "valid",
			},
		];

		mockFindMany.mockResolvedValue([]);

		const result = await evaluateDeterministicRules(incoming, "person-1");
		expect(result.decision).toBeNull();
	});

	it("does not hard-merge unverified government ID match", async () => {
		const incoming: NormalizedIdentifier[] = [
			{
				type: "government_id_hash",
				rawValue: "S1234567A",
				normalizedValue: null,
				hashedValue: "hash_abc",
				isVerified: false,
				qualityFlag: "valid",
			},
		];

		mockFindMany.mockResolvedValue([
			{
				personIdentifierId: "pid-1",
				personId: "person-1",
				sourceRecordPk: null,
				sourceSystemId: "ss-1",
				identifierType: "government_id_hash",
				rawValue: null,
				normalizedValue: null,
				hashedValue: "hash_abc",
				isVerified: false,
				verificationMethod: null,
				isActive: true,
				qualityFlag: "valid",
				firstSeenAt: new Date(),
				lastSeenAt: new Date(),
				lastConfirmedAt: new Date(),
				metadata: {},
			},
		] as any);

		const result = await evaluateDeterministicRules(incoming, "person-1");
		// Should NOT hard merge — both need to be verified
		expect(result.decision).toBeNull();
		expect(result.reasons).toContain("Government ID match (unverified — not a hard merge)");
	});
});
