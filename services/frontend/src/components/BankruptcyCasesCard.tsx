"use client";

import type { ReactElement } from "react";

import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import type { PersonBankruptcyCase } from "@/lib/api-types-person";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

function displayValue(value: string | null): string {
  return value && value.trim() ? value : "—";
}

function displayDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString();
}

interface Props {
  personId: string;
}

export default function BankruptcyCasesCard({ personId }: Props): ReactElement {
  const { rows, error, loading, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonBankruptcyCase>(
      `/bff/persons/${encodeURIComponent(personId)}/bankruptcy-cases`,
    );

  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 2 }}>
        <Typography variant="h6">Bankruptcy Cases</Typography>
        {loading ? <CircularProgress size={18} /> : null}
      </Stack>
      {error !== null ? (
        <Alert severity="error">{error}</Alert>
      ) : rows === null ? null : rows.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No bankruptcy cases found.
        </Typography>
      ) : (
        <>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Case #</TableCell>
                <TableCell>Event</TableCell>
                <TableCell>Event date</TableCell>
                <TableCell>Document</TableCell>
                <TableCell>Document date</TableCell>
                <TableCell>Trustee</TableCell>
                <TableCell>Source</TableCell>
                <TableCell>Seen</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((c) => (
                <TableRow key={c.bankruptcy_case_id} hover>
                  <TableCell>{displayValue(c.case_number ?? c.source_case_id)}</TableCell>
                  <TableCell>{displayValue(c.event_type)}</TableCell>
                  <TableCell>{displayDate(c.event_date)}</TableCell>
                  <TableCell>{displayValue(c.document_type)}</TableCell>
                  <TableCell>{displayDate(c.document_date)}</TableCell>
                  <TableCell>{displayValue(c.trustee_name ?? c.trustee_firm)}</TableCell>
                  <TableCell>
                    {c.source_url ? (
                      <Link href={c.source_url} target="_blank" rel="noreferrer">
                        {c.source_system_key}
                      </Link>
                    ) : (
                      c.source_system_key
                    )}
                  </TableCell>
                  <TableCell>
                    {displayDate(c.first_seen_at)} / {displayDate(c.last_seen_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <PaginationBar
            from={from}
            to={to}
            total={total}
            hasPrev={hasPrev}
            hasNext={hasNext}
            loading={loading}
            onPrev={goPrev}
            onNext={goNext}
          />
        </>
      )}
    </Paper>
  );
}
