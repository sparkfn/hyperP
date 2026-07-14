export function personDisplayName(displayName: string | null | undefined): string {
  return displayName?.trim() || "Unnamed person";
}

export function shortReference(value: string, label: string): string {
  const displayValue = value.length > 8 ? `${value.slice(0, 8)}…` : value;
  return `${label}: ${displayValue}`;
}
