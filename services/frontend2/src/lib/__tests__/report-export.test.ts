import { describe, expect, it } from "vitest";

import type { ReportResult } from "../api-types";
import { toCsv, toInsertSql, toJson, toTsv } from "../report-export";

function report(columns: string[], rows: ReportResult["rows"]): ReportResult {
  return { columns, rows, row_count: rows.length };
}

describe("toCsv", () => {
  it("quotes delimiters, quotes, CRLF, and Unicode without losing data", () => {
    const result = report(
      ["name", "note"],
      [{ name: "José, Jr.", note: "first\r\n\"quoted\" line" }],
    );

    expect(toCsv(result)).toBe(
      'name,note\n"José, Jr.","first\r\n""quoted"" line"',
    );
  });

  it.each(["=2+2", "+SUM(A1:A2)", "-1+1", "@cmd"]) (
    "neutralizes spreadsheet formula value %s",
    (formula) => {
      expect(toCsv(report(["value"], [{ value: formula }]))).toBe(
        `value\n'${formula}`,
      );
    },
  );

  it("preserves null as an empty cell", () => {
    expect(toCsv(report(["value"], [{ value: null }]))).toBe("value\n");
  });
});

describe("toTsv", () => {
  it("removes tabs and all newline forms from cell values", () => {
    const result = report(["value"], [{ value: "a\tb\r\nc\rd" }]);

    expect(toTsv(result)).toBe("value\na b c d");
  });

  it.each(["=2+2", "+SUM(A1:A2)", "-1+1", "@cmd"]) (
    "neutralizes spreadsheet formula value %s",
    (formula) => {
      expect(toTsv(report(["value"], [{ value: formula }]))).toBe(
        `value\n'${formula}`,
      );
    },
  );
});

describe("toInsertSql", () => {
  it("quotes identifiers and string literals", () => {
    const result = report(['full"name'], [{ 'full"name': "O'Brien" }]);

    expect(toInsertSql(result, 'people"archive')).toBe(
      'INSERT INTO "people""archive" ("full""name") VALUES (\'O\'\'Brien\');',
    );
  });

  it("rejects non-finite numbers instead of emitting invalid SQL", () => {
    const result = report(["score"], [{ score: Number.POSITIVE_INFINITY }]);

    expect(() => toInsertSql(result, "people")).toThrowError(
      "Cannot export a non-finite number to SQL.",
    );
  });
});

describe("toJson", () => {
  it("serializes rows without the report metadata", () => {
    expect(toJson(report(["active"], [{ active: true }]))).toBe(
      '[\n  {\n    "active": true\n  }\n]',
    );
  });
});
