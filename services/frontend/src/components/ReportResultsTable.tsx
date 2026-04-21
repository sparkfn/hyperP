"use client";

import { useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Typography from "@mui/material/Typography";
import FileDownloadIcon from "@mui/icons-material/FileDownload";

import type { ReportResult } from "@/lib/api-types";
import { downloadFile, toCsv, toInsertSql, toJson, toTsv } from "@/lib/report-export";

type SortOrder = "asc" | "desc";

interface ReportResultsTableProps {
  result: ReportResult;
  tableName?: string;
}

function compareValues(
  a: string | number | boolean | null,
  b: string | number | boolean | null,
  order: SortOrder,
): number {
  if (a === null && b === null) return 0;
  if (a === null) return order === "asc" ? 1 : -1;
  if (b === null) return order === "asc" ? -1 : 1;
  if (typeof a === "number" && typeof b === "number") {
    return order === "asc" ? a - b : b - a;
  }
  const strA = String(a);
  const strB = String(b);
  return order === "asc" ? strA.localeCompare(strB) : strB.localeCompare(strA);
}

function formatCell(value: string | number | boolean | null): string {
  if (value === null) return "\u2014";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

export default function ReportResultsTable({
  result,
  tableName = "report_data",
}: ReportResultsTableProps): ReactElement {
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortOrder, setSortOrder] = useState<SortOrder>("asc");
  const [page, setPage] = useState<number>(0);
  const [rowsPerPage, setRowsPerPage] = useState<number>(25);
  const [exportAnchor, setExportAnchor] = useState<HTMLElement | null>(null);

  function handleSort(col: string): void {
    const newOrder: SortOrder =
      sortCol === col && sortOrder === "asc" ? "desc" : "asc";
    setSortCol(col);
    setSortOrder(newOrder);
  }

  function handleExport(format: "csv" | "tsv" | "sql" | "json"): void {
    setExportAnchor(null);
    if (format === "csv") {
      downloadFile(toCsv(result), `${tableName}.csv`, "text/csv");
    } else if (format === "tsv") {
      downloadFile(toTsv(result), `${tableName}.txt`, "text/tab-separated-values");
    } else if (format === "sql") {
      downloadFile(toInsertSql(result, tableName), `${tableName}.sql`, "application/sql");
    } else {
      downloadFile(toJson(result), `${tableName}.json`, "application/json");
    }
  }

  const sorted = sortCol
    ? [...result.rows].sort((a, b) =>
        compareValues(a[sortCol] ?? null, b[sortCol] ?? null, sortOrder),
      )
    : result.rows;

  const paged = sorted.slice(page * rowsPerPage, (page + 1) * rowsPerPage);

  return (
    <Paper elevation={0} variant="outlined">
      <Stack
        direction="row"
        justifyContent="space-between"
        alignItems="center"
        sx={{ px: 2, pt: 1.5, pb: 0.5 }}
      >
        <Typography variant="body2" color="text.secondary">
          {result.row_count} row{result.row_count !== 1 ? "s" : ""} returned
        </Typography>
        <Button
          size="small"
          startIcon={<FileDownloadIcon />}
          onClick={(e) => setExportAnchor(e.currentTarget)}
        >
          Export
        </Button>
        <Menu
          anchorEl={exportAnchor}
          open={exportAnchor !== null}
          onClose={() => setExportAnchor(null)}
        >
          <MenuItem onClick={() => handleExport("csv")}>CSV (.csv)</MenuItem>
          <MenuItem onClick={() => handleExport("tsv")}>Delimited Text (.txt)</MenuItem>
          <MenuItem onClick={() => handleExport("sql")}>INSERT SQL (.sql)</MenuItem>
          <MenuItem onClick={() => handleExport("json")}>JSON (.json)</MenuItem>
        </Menu>
      </Stack>
      <Box sx={{ overflow: "auto", maxHeight: "70vh" }}>
        <Table size="small" stickyHeader sx={{ minWidth: result.columns.length * 150 }}>
          <TableHead>
            <TableRow>
              {result.columns.map((col) => (
                <TableCell key={col} sx={{ whiteSpace: "nowrap" }}>
                  <TableSortLabel
                    active={sortCol === col}
                    direction={sortCol === col ? sortOrder : "asc"}
                    onClick={() => handleSort(col)}
                  >
                    {col}
                  </TableSortLabel>
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {paged.map((row, idx) => (
              <TableRow key={idx} hover>
                {result.columns.map((col) => (
                  <TableCell key={col} sx={{ whiteSpace: "nowrap" }}>
                    {formatCell(row[col] ?? null)}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </Box>
      <TablePagination
        component="div"
        count={result.row_count}
        page={page}
        rowsPerPage={rowsPerPage}
        onPageChange={(_, newPage) => setPage(newPage)}
        onRowsPerPageChange={(e) => {
          setRowsPerPage(parseInt(e.target.value, 10));
          setPage(0);
        }}
        rowsPerPageOptions={[10, 25, 50, 100]}
      />
    </Paper>
  );
}
