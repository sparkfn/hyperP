export interface PersonListColumn {
  key: string;
  minWidth: number;
  resizable: boolean;
}

export const PERSON_LIST_COLUMNS: readonly PersonListColumn[] = [
  { key: "check", minWidth: 36, resizable: false },
  { key: "name", minWidth: 360, resizable: true },
  { key: "nric", minWidth: 80, resizable: true },
  { key: "phone", minWidth: 112, resizable: true },
  { key: "dob", minWidth: 90, resizable: true },
  { key: "email", minWidth: 100, resizable: true },
  { key: "address", minWidth: 96, resizable: true },
  { key: "entity", minWidth: 80, resizable: true },
  { key: "relations", minWidth: 96, resizable: true },
  { key: "orders", minWidth: 84, resizable: true },
  { key: "deals", minWidth: 72, resizable: true },
  { key: "matches", minWidth: 88, resizable: true },
  { key: "decisionHistory", minWidth: 112, resizable: true },
  { key: "quality", minWidth: 108, resizable: true },
  { key: "graph", minWidth: 48, resizable: false },
] as const;

export const PERSON_LIST_DEFAULT_WIDTHS: readonly number[] = [
  36, 480, 110, 132, 110, 160, 140, 120, 112, 84, 72, 88, 112, 124, 54,
] as const;

export const PERSON_LIST_TABLE_COLUMN_COUNT = PERSON_LIST_COLUMNS.length;
