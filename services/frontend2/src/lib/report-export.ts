/**
 * Pure functions to convert ReportResult data into downloadable formats.
 * All functions are client-side only — no server interaction needed.
 */

import type { ReportResult } from "./api-types";

type CellValue = string | number | boolean | null;

function neutralizeSpreadsheetFormula(value: CellValue): string {
  const raw = String(value);
  return typeof value === "string" && /^[=+\-@]/.test(raw) ? `'${raw}` : raw;
}

// ---------------------------------------------------------------------------
// CSV
// ---------------------------------------------------------------------------

function escapeCsv(value: CellValue): string {
  if (value === null) return "";
  const str = neutralizeSpreadsheetFormula(value);
  if (str.includes(",") || str.includes('"') || /[\r\n]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

export function toCsv(result: ReportResult): string {
  const header = result.columns.map(escapeCsv).join(",");
  const rows = result.rows.map((row) =>
    result.columns.map((col) => escapeCsv(row[col] ?? null)).join(","),
  );
  return [header, ...rows].join("\n");
}

// ---------------------------------------------------------------------------
// Delimited text (tab-separated)
// ---------------------------------------------------------------------------

function escapeTsv(value: CellValue): string {
  if (value === null) return "";
  return neutralizeSpreadsheetFormula(value).replace(/[\t\r\n]+/g, " ");
}

export function toTsv(result: ReportResult): string {
  const header = result.columns.map(escapeTsv).join("\t");
  const rows = result.rows.map((row) =>
    result.columns.map((col) => escapeTsv(row[col] ?? null)).join("\t"),
  );
  return [header, ...rows].join("\n");
}

// ---------------------------------------------------------------------------
// INSERT SQL
// ---------------------------------------------------------------------------

function sqlLiteral(value: CellValue): string {
  if (value === null) return "NULL";
  if (typeof value === "boolean") return value ? "TRUE" : "FALSE";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error("Cannot export a non-finite number to SQL.");
    }
    return String(value);
  }
  const escaped = String(value).replace(/'/g, "''");
  return `'${escaped}'`;
}

function sqlIdentifier(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

export function toInsertSql(result: ReportResult, tableName: string): string {
  if (result.rows.length === 0) return `-- No rows to insert into ${tableName}`;
  const cols = result.columns.map(sqlIdentifier).join(", ");
  const lines = result.rows.map((row) => {
    const values = result.columns.map((col) => sqlLiteral(row[col] ?? null)).join(", ");
    return `INSERT INTO ${sqlIdentifier(tableName)} (${cols}) VALUES (${values});`;
  });
  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// JSON
// ---------------------------------------------------------------------------

export function toJson(result: ReportResult): string {
  return JSON.stringify(result.rows, null, 2);
}

// ---------------------------------------------------------------------------
// Download trigger
// ---------------------------------------------------------------------------

export function downloadFile(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
