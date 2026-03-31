import { parsePhoneNumberFromString } from "libphonenumber-js";

export interface NormalizedIdentifier {
	type: string;
	rawValue: string;
	normalizedValue: string | null;
	hashedValue: string | null;
	isVerified: boolean;
	qualityFlag: "valid" | "invalid_format" | "placeholder_value";
}

export interface NormalizedAttributes {
	[key: string]: {
		rawValue: string;
		normalizedValue: string;
		qualityFlag: "valid" | "invalid_format" | "placeholder_value";
	};
}

const PLACEHOLDER_PATTERNS = [
	/^n\/?a$/i,
	/^none$/i,
	/^null$/i,
	/^undefined$/i,
	/^unknown$/i,
	/^-+$/,
	/^\.+$/,
	/^x+$/i,
	/^test$/i,
	/^dummy$/i,
	/^0{5,}$/,
];

function isPlaceholder(value: string): boolean {
	const trimmed = value.trim();
	if (trimmed.length === 0) return true;
	return PLACEHOLDER_PATTERNS.some((p) => p.test(trimmed));
}

/**
 * Normalize phone to E.164.
 * Default country: SG (Singapore) per project context.
 */
export function normalizePhone(raw: string): {
	normalized: string | null;
	qualityFlag: "valid" | "invalid_format" | "placeholder_value";
} {
	if (isPlaceholder(raw)) {
		return { normalized: null, qualityFlag: "placeholder_value" };
	}

	const parsed = parsePhoneNumberFromString(raw, "SG");
	if (!parsed || !parsed.isValid()) {
		return { normalized: null, qualityFlag: "invalid_format" };
	}

	return { normalized: parsed.format("E.164"), qualityFlag: "valid" };
}

/**
 * Normalize email: lowercase, trim, remove dots in gmail local part.
 */
export function normalizeEmail(raw: string): {
	normalized: string | null;
	qualityFlag: "valid" | "invalid_format" | "placeholder_value";
} {
	if (isPlaceholder(raw)) {
		return { normalized: null, qualityFlag: "placeholder_value" };
	}

	const trimmed = raw.trim().toLowerCase();
	const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
	if (!emailRegex.test(trimmed)) {
		return { normalized: null, qualityFlag: "invalid_format" };
	}

	return { normalized: trimmed, qualityFlag: "valid" };
}

/**
 * Normalize name: trim, collapse whitespace, normalize unicode punctuation.
 */
export function normalizeName(raw: string): {
	normalized: string | null;
	qualityFlag: "valid" | "invalid_format" | "placeholder_value";
} {
	if (isPlaceholder(raw)) {
		return { normalized: null, qualityFlag: "placeholder_value" };
	}

	const normalized = raw
		.trim()
		.replace(/\s+/g, " ") // collapse whitespace
		.replace(/[\u2018\u2019\u201B]/g, "'") // normalize apostrophes
		.replace(/[\u201C\u201D\u201E]/g, '"'); // normalize quotes

	if (normalized.length === 0) {
		return { normalized: null, qualityFlag: "placeholder_value" };
	}

	return { normalized, qualityFlag: "valid" };
}

/**
 * Normalize DOB to ISO date string (YYYY-MM-DD).
 */
export function normalizeDob(raw: string): {
	normalized: string | null;
	qualityFlag: "valid" | "invalid_format" | "placeholder_value";
} {
	if (isPlaceholder(raw)) {
		return { normalized: null, qualityFlag: "placeholder_value" };
	}

	const trimmed = raw.trim();

	// Try ISO format first
	const isoMatch = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})/);
	if (isoMatch) {
		const date = new Date(`${isoMatch[1]}-${isoMatch[2]}-${isoMatch[3]}T00:00:00Z`);
		if (!Number.isNaN(date.getTime())) {
			return { normalized: `${isoMatch[1]}-${isoMatch[2]}-${isoMatch[3]}`, qualityFlag: "valid" };
		}
	}

	// Try DD/MM/YYYY or DD-MM-YYYY
	const dmyMatch = trimmed.match(/^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$/);
	if (dmyMatch) {
		const day = dmyMatch[1].padStart(2, "0");
		const month = dmyMatch[2].padStart(2, "0");
		const year = dmyMatch[3];
		const date = new Date(`${year}-${month}-${day}T00:00:00Z`);
		if (!Number.isNaN(date.getTime())) {
			return { normalized: `${year}-${month}-${day}`, qualityFlag: "valid" };
		}
	}

	return { normalized: null, qualityFlag: "invalid_format" };
}

/**
 * Normalize a generic attribute value (trim, placeholder check).
 */
export function normalizeAttribute(raw: string): {
	normalized: string | null;
	qualityFlag: "valid" | "invalid_format" | "placeholder_value";
} {
	if (isPlaceholder(raw)) {
		return { normalized: null, qualityFlag: "placeholder_value" };
	}
	return { normalized: raw.trim(), qualityFlag: "valid" };
}

/**
 * Normalize a single identifier based on its type.
 */
export function normalizeIdentifier(
	type: string,
	rawValue: string,
	isVerified: boolean,
): NormalizedIdentifier {
	let result: { normalized: string | null; qualityFlag: "valid" | "invalid_format" | "placeholder_value" };

	switch (type) {
		case "phone":
			result = normalizePhone(rawValue);
			break;
		case "email":
			result = normalizeEmail(rawValue);
			break;
		default:
			result = normalizeAttribute(rawValue);
			break;
	}

	return {
		type,
		rawValue,
		normalizedValue: result.normalized,
		hashedValue: null,
		isVerified,
		qualityFlag: result.qualityFlag,
	};
}

/**
 * Normalize all attributes from a raw payload.
 */
export function normalizeAttributes(
	attributes: Record<string, string>,
): NormalizedAttributes {
	const normalized: NormalizedAttributes = {};

	for (const [key, rawValue] of Object.entries(attributes)) {
		if (rawValue == null) continue;
		const raw = String(rawValue);

		switch (key) {
			case "full_name": {
				const r = normalizeName(raw);
				normalized[key] = { rawValue: raw, normalizedValue: r.normalized ?? "", qualityFlag: r.qualityFlag };
				break;
			}
			case "dob": {
				const r = normalizeDob(raw);
				normalized[key] = { rawValue: raw, normalizedValue: r.normalized ?? "", qualityFlag: r.qualityFlag };
				break;
			}
			default: {
				const r = normalizeAttribute(raw);
				normalized[key] = { rawValue: raw, normalizedValue: r.normalized ?? "", qualityFlag: r.qualityFlag };
				break;
			}
		}
	}

	return normalized;
}
