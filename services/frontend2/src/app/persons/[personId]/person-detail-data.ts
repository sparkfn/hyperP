import type { PersonIdentifier } from "@/lib/api-types-person";

/**
 * Append identifier cursor pages exactly in server order. Identifiers sharing
 * a type/value can still carry distinct relationship provenance, so client
 * coalescing would silently lose valid API rows.
 */
export function mergeIdentifierPage(
  existing: PersonIdentifier[],
  page: PersonIdentifier[],
): PersonIdentifier[] {
  return [...existing, ...page];
}
