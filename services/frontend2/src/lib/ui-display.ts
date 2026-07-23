export function personDisplayName(displayName: string | null | undefined): string {
  return displayName?.trim() || "Unnamed person";
}

export function shortReference(value: string, label: string): string {
  const displayValue = value.length > 8 ? `${value.slice(0, 8)}…` : value;
  return `${label}: ${displayValue}`;
}

const SOURCE_RECORD_PREFIXES: Readonly<Record<string, string>> = {
  fundbox: "fundbox-",
  "fundbox:contacts": "fundbox-",
  "fundbox:legacy": "fundbox-",
  "fundbox:merged": "fundbox-",
  "fundbox:sales": "fundbox-",
  speedzone_phppos: "speedzone_phppos-",
  "speedzone_phppos:sales": "speedzone_phppos-",
  eko_phppos: "eko_phppos-",
  "eko_phppos:sales": "eko_phppos-",
  whatsapp_chat: "whatsapp-",
  bitrix_chat: "bitrix-",
  onediver: "onediver-",
  "onediver:sales": "onediver-",
};

export function sourceRecordReference(
  value: string | null | undefined,
  sourceSystemKey?: string,
): string {
  if (value === null || value === undefined || value === "" || sourceSystemKey === undefined) {
    return value ?? "";
  }
  const prefix = SOURCE_RECORD_PREFIXES[sourceSystemKey];
  return prefix !== undefined && value.startsWith(prefix) ? value.slice(prefix.length) : value;
}
