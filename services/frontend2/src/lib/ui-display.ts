export function personDisplayName(displayName: string | null | undefined): string {
  return displayName?.trim() || "Unnamed person";
}

export function shortReference(value: string, label: string): string {
  const displayValue = value.length > 8 ? `${value.slice(0, 8)}…` : value;
  return `${label}: ${displayValue}`;
}

const SHORTENABLE_SOURCE_PREFIXES: ReadonlySet<string> = new Set(["BITRIX", "SPZ"]);

export function sourceRecordReference(value: string): string {
  const segments = value.split("-");
  const prefix = segments[0];
  return segments.length === 3 && prefix !== undefined && SHORTENABLE_SOURCE_PREFIXES.has(prefix)
    ? segments.slice(1).join("-")
    : value;
}
