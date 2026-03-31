import { describe, it, expect } from "vitest";
import {
	normalizePhone,
	normalizeEmail,
	normalizeName,
	normalizeDob,
	normalizeIdentifier,
	normalizeAttributes,
} from "../../src/modules/ingestion/normalizer.js";

describe("normalizePhone", () => {
	it("normalizes a valid SG phone to E.164", () => {
		const result = normalizePhone("91234567");
		expect(result.normalized).toBe("+6591234567");
		expect(result.qualityFlag).toBe("valid");
	});

	it("normalizes a phone with country code", () => {
		const result = normalizePhone("+6591234567");
		expect(result.normalized).toBe("+6591234567");
		expect(result.qualityFlag).toBe("valid");
	});

	it("flags invalid phone", () => {
		const result = normalizePhone("123");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("invalid_format");
	});

	it("detects placeholder phone", () => {
		const result = normalizePhone("N/A");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("placeholder_value");
	});

	it("detects all-zero placeholder", () => {
		const result = normalizePhone("00000000");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("placeholder_value");
	});
});

describe("normalizeEmail", () => {
	it("lowercases and trims email", () => {
		const result = normalizeEmail("  Alice@Example.COM  ");
		expect(result.normalized).toBe("alice@example.com");
		expect(result.qualityFlag).toBe("valid");
	});

	it("flags invalid email", () => {
		const result = normalizeEmail("not-an-email");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("invalid_format");
	});

	it("detects placeholder email", () => {
		const result = normalizeEmail("unknown");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("placeholder_value");
	});
});

describe("normalizeName", () => {
	it("trims and collapses whitespace", () => {
		const result = normalizeName("  Alice   Tan  ");
		expect(result.normalized).toBe("Alice Tan");
		expect(result.qualityFlag).toBe("valid");
	});

	it("normalizes smart quotes and apostrophes", () => {
		const result = normalizeName("O\u2019Brien");
		expect(result.normalized).toBe("O'Brien");
	});

	it("detects placeholder name", () => {
		const result = normalizeName("N/A");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("placeholder_value");
	});

	it("detects dash-only name", () => {
		const result = normalizeName("---");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("placeholder_value");
	});
});

describe("normalizeDob", () => {
	it("parses ISO date", () => {
		const result = normalizeDob("1989-10-01");
		expect(result.normalized).toBe("1989-10-01");
		expect(result.qualityFlag).toBe("valid");
	});

	it("parses DD/MM/YYYY", () => {
		const result = normalizeDob("01/10/1989");
		expect(result.normalized).toBe("1989-10-01");
		expect(result.qualityFlag).toBe("valid");
	});

	it("parses D-M-YYYY", () => {
		const result = normalizeDob("1-3-1990");
		expect(result.normalized).toBe("1990-03-01");
		expect(result.qualityFlag).toBe("valid");
	});

	it("flags invalid date", () => {
		const result = normalizeDob("not a date");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("invalid_format");
	});

	it("detects placeholder", () => {
		const result = normalizeDob("N/A");
		expect(result.normalized).toBeNull();
		expect(result.qualityFlag).toBe("placeholder_value");
	});
});

describe("normalizeIdentifier", () => {
	it("dispatches phone normalization", () => {
		const result = normalizeIdentifier("phone", "+6591234567", false);
		expect(result.normalizedValue).toBe("+6591234567");
		expect(result.rawValue).toBe("+6591234567");
		expect(result.isVerified).toBe(false);
	});

	it("dispatches email normalization", () => {
		const result = normalizeIdentifier("email", "Alice@Example.com", true);
		expect(result.normalizedValue).toBe("alice@example.com");
		expect(result.isVerified).toBe(true);
	});

	it("falls back to generic normalization for unknown types", () => {
		const result = normalizeIdentifier("loyalty_id", " ABC-123 ", false);
		expect(result.normalizedValue).toBe("ABC-123");
	});
});

describe("normalizeAttributes", () => {
	it("normalizes a full attribute set", () => {
		const result = normalizeAttributes({
			full_name: "  Alice  Tan ",
			dob: "01/10/1989",
			address: "10 Example Street",
		});

		expect(result.full_name.normalizedValue).toBe("Alice Tan");
		expect(result.full_name.qualityFlag).toBe("valid");
		expect(result.dob.normalizedValue).toBe("1989-10-01");
		expect(result.dob.qualityFlag).toBe("valid");
		expect(result.address.normalizedValue).toBe("10 Example Street");
	});

	it("flags placeholder attributes", () => {
		const result = normalizeAttributes({
			full_name: "N/A",
			dob: "unknown",
		});

		expect(result.full_name.qualityFlag).toBe("placeholder_value");
		expect(result.dob.qualityFlag).toBe("placeholder_value");
	});
});
