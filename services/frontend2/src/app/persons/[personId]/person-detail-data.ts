import type { PersonIdentifier } from "@/lib/api-types-person";

function identifierKey(identifier: PersonIdentifier): string {
  return `${identifier.identifier_type}\u0000${identifier.normalized_value}`;
}

/**
 * Append an identifier cursor page without duplicating a boundary item.
 * The API ordering is stable by active status, type, and value, so the
 * identifier identity is sufficient to preserve all reachable rows.
 */
export function mergeIdentifierPage(
  existing: PersonIdentifier[],
  page: PersonIdentifier[],
): PersonIdentifier[] {
  const seen = new Set(existing.map(identifierKey));
  const additions = page.filter((identifier) => {
    const key = identifierKey(identifier);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return [...existing, ...additions];
}
