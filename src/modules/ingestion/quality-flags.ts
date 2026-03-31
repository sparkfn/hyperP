import type { QualityFlag } from "../../generated/prisma/client.js";

/**
 * Known shared/company phone numbers that should not be used for matching.
 * In production, this would be loaded from a config table.
 */
const KNOWN_SHARED_PHONES = new Set<string>([
	// placeholder — add known shared business lines here
]);

/**
 * Known generic/catch-all email domains.
 */
const GENERIC_EMAIL_PATTERNS = [
	/^(info|admin|contact|support|noreply|no-reply|test)@/i,
];

export function assessPhoneQuality(normalizedValue: string | null, rawValue: string): QualityFlag {
	if (!normalizedValue) return "invalid_format";
	if (KNOWN_SHARED_PHONES.has(normalizedValue)) return "shared_identifier_suspected";
	return "valid";
}

export function assessEmailQuality(normalizedValue: string | null, rawValue: string): QualityFlag {
	if (!normalizedValue) return "invalid_format";
	if (GENERIC_EMAIL_PATTERNS.some((p) => p.test(normalizedValue))) {
		return "shared_identifier_suspected";
	}
	return "valid";
}

export function assessIdentifierQuality(
	type: string,
	normalizedValue: string | null,
	rawValue: string,
): QualityFlag {
	switch (type) {
		case "phone":
			return assessPhoneQuality(normalizedValue, rawValue);
		case "email":
			return assessEmailQuality(normalizedValue, rawValue);
		default:
			return normalizedValue ? "valid" : "invalid_format";
	}
}
