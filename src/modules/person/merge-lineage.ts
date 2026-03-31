/**
 * Merge lineage format:
 * absorbed_person_id|merge_event_id|actor|timestamp;...
 */

export interface LineageEntry {
	absorbedPersonId: string;
	mergeEventId: string;
	actor: string;
	timestamp: string;
}

export function parseLineage(lineage: string | null): LineageEntry[] {
	if (!lineage || lineage.trim() === "") return [];

	return lineage.split(";").filter(Boolean).map((segment) => {
		const [absorbedPersonId, mergeEventId, actor, timestamp] = segment.split("|");
		return { absorbedPersonId, mergeEventId, actor, timestamp };
	});
}

export function appendLineage(
	existing: string | null,
	entry: LineageEntry,
): string {
	const segment = `${entry.absorbedPersonId}|${entry.mergeEventId}|${entry.actor}|${entry.timestamp}`;
	if (!existing || existing.trim() === "") return segment;
	return `${existing};${segment}`;
}

export function removeLineageEntry(
	existing: string | null,
	mergeEventId: string,
): string {
	const entries = parseLineage(existing);
	const filtered = entries.filter((e) => e.mergeEventId !== mergeEventId);
	return filtered
		.map((e) => `${e.absorbedPersonId}|${e.mergeEventId}|${e.actor}|${e.timestamp}`)
		.join(";");
}
