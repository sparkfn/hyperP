import { describe, it, expect } from "vitest";
import {
	assessPhoneQuality,
	assessEmailQuality,
	assessIdentifierQuality,
} from "../../src/modules/ingestion/quality-flags.js";

describe("assessPhoneQuality", () => {
	it("returns valid for a normal phone", () => {
		expect(assessPhoneQuality("+6591234567", "91234567")).toBe("valid");
	});

	it("returns invalid_format for null normalized value", () => {
		expect(assessPhoneQuality(null, "bad")).toBe("invalid_format");
	});
});

describe("assessEmailQuality", () => {
	it("returns valid for a normal email", () => {
		expect(assessEmailQuality("alice@example.com", "alice@example.com")).toBe("valid");
	});

	it("flags generic emails as shared_identifier_suspected", () => {
		expect(assessEmailQuality("info@company.com", "info@company.com")).toBe(
			"shared_identifier_suspected",
		);
		expect(assessEmailQuality("noreply@company.com", "noreply@company.com")).toBe(
			"shared_identifier_suspected",
		);
		expect(assessEmailQuality("support@company.com", "support@company.com")).toBe(
			"shared_identifier_suspected",
		);
	});

	it("returns invalid_format for null normalized value", () => {
		expect(assessEmailQuality(null, "bad")).toBe("invalid_format");
	});
});

describe("assessIdentifierQuality", () => {
	it("dispatches to phone assessment", () => {
		expect(assessIdentifierQuality("phone", "+6591234567", "91234567")).toBe("valid");
	});

	it("dispatches to email assessment", () => {
		expect(assessIdentifierQuality("email", "info@co.com", "info@co.com")).toBe(
			"shared_identifier_suspected",
		);
	});

	it("returns valid for other types with normalized value", () => {
		expect(assessIdentifierQuality("loyalty_id", "ABC-123", "ABC-123")).toBe("valid");
	});

	it("returns invalid_format for other types without normalized value", () => {
		expect(assessIdentifierQuality("loyalty_id", null, "")).toBe("invalid_format");
	});
});
