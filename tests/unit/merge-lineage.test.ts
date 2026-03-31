import { describe, it, expect } from "vitest";
import {
	parseLineage,
	appendLineage,
	removeLineageEntry,
} from "../../src/modules/person/merge-lineage.js";

describe("parseLineage", () => {
	it("returns empty array for null", () => {
		expect(parseLineage(null)).toEqual([]);
	});

	it("returns empty array for empty string", () => {
		expect(parseLineage("")).toEqual([]);
	});

	it("parses a single entry", () => {
		const result = parseLineage("person-1|event-1|system:engine|2026-01-01T00:00:00Z");
		expect(result).toEqual([
			{
				absorbedPersonId: "person-1",
				mergeEventId: "event-1",
				actor: "system:engine",
				timestamp: "2026-01-01T00:00:00Z",
			},
		]);
	});

	it("parses multiple entries", () => {
		const lineage =
			"p1|e1|system:engine|2026-01-01T00:00:00Z;p2|e2|admin:alice|2026-02-01T00:00:00Z";
		const result = parseLineage(lineage);
		expect(result).toHaveLength(2);
		expect(result[0].absorbedPersonId).toBe("p1");
		expect(result[1].absorbedPersonId).toBe("p2");
	});
});

describe("appendLineage", () => {
	it("creates lineage from null", () => {
		const result = appendLineage(null, {
			absorbedPersonId: "p1",
			mergeEventId: "e1",
			actor: "system:engine",
			timestamp: "2026-01-01T00:00:00Z",
		});
		expect(result).toBe("p1|e1|system:engine|2026-01-01T00:00:00Z");
	});

	it("appends to existing lineage", () => {
		const existing = "p1|e1|system:engine|2026-01-01T00:00:00Z";
		const result = appendLineage(existing, {
			absorbedPersonId: "p2",
			mergeEventId: "e2",
			actor: "admin:alice",
			timestamp: "2026-02-01T00:00:00Z",
		});
		expect(result).toBe(
			"p1|e1|system:engine|2026-01-01T00:00:00Z;p2|e2|admin:alice|2026-02-01T00:00:00Z",
		);
	});
});

describe("removeLineageEntry", () => {
	it("removes an entry by merge event ID", () => {
		const lineage =
			"p1|e1|system:engine|2026-01-01T00:00:00Z;p2|e2|admin:alice|2026-02-01T00:00:00Z";
		const result = removeLineageEntry(lineage, "e1");
		expect(result).toBe("p2|e2|admin:alice|2026-02-01T00:00:00Z");
	});

	it("returns empty string when removing the only entry", () => {
		const lineage = "p1|e1|system:engine|2026-01-01T00:00:00Z";
		const result = removeLineageEntry(lineage, "e1");
		expect(result).toBe("");
	});

	it("returns original when event ID not found", () => {
		const lineage = "p1|e1|system:engine|2026-01-01T00:00:00Z";
		const result = removeLineageEntry(lineage, "e99");
		expect(result).toBe("p1|e1|system:engine|2026-01-01T00:00:00Z");
	});
});
