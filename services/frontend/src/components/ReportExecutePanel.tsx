"use client";

import { useState, type FormEvent, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { ReportDetail, ReportParamType, ReportResult } from "@/lib/api-types";
import ReportResultsTable from "@/components/ReportResultsTable";

interface ReportExecutePanelProps {
  report: ReportDetail;
}

export default function ReportExecutePanel({ report }: ReportExecutePanelProps): ReactElement {
  const [paramValues, setParamValues] = useState<Record<string, string>>(() => {
    const defaults: Record<string, string> = {};
    for (const p of report.parameters) {
      defaults[p.name] = p.default_value ?? "";
    }
    return defaults;
  });
  const [executing, setExecuting] = useState<boolean>(false);
  const [execError, setExecError] = useState<string | null>(null);
  const [result, setResult] = useState<ReportResult | null>(null);

  async function handleExecute(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setExecuting(true);
    setExecError(null);
    setResult(null);

    const coerced: Record<string, string | number | boolean | null> = {};
    for (const pdef of report.parameters) {
      const raw = paramValues[pdef.name] ?? "";
      if (raw === "" && !pdef.required) {
        coerced[pdef.name] = pdef.default_value ?? null;
        continue;
      }
      if (pdef.param_type === "integer") {
        coerced[pdef.name] = parseInt(raw, 10);
      } else if (pdef.param_type === "float") {
        coerced[pdef.name] = parseFloat(raw);
      } else if (pdef.param_type === "boolean") {
        coerced[pdef.name] = raw.toLowerCase() === "true";
      } else {
        coerced[pdef.name] = raw;
      }
    }

    try {
      const data = await bffFetch<ReportResult>(
        `/bff/reports/${encodeURIComponent(report.report_key)}/execute`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ parameters: coerced }),
        },
      );
      setResult(data);
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Report execution failed.";
      setExecError(msg);
    } finally {
      setExecuting(false);
    }
  }

  return (
    <>
      {report.parameters.length > 0 ? (
        <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
          <Typography variant="subtitle2" sx={{ mb: 2 }}>
            Parameters
          </Typography>
          <form onSubmit={(e) => void handleExecute(e)}>
            <Stack spacing={2}>
              {report.parameters.map((pdef) => (
                <ExecuteParamField
                  key={pdef.name}
                  name={pdef.name}
                  label={pdef.label}
                  paramType={pdef.param_type}
                  required={pdef.required}
                  value={paramValues[pdef.name] ?? ""}
                  onChange={(val) =>
                    setParamValues((prev) => ({ ...prev, [pdef.name]: val }))
                  }
                />
              ))}
              <Box>
                <Button type="submit" variant="contained" disabled={executing}>
                  {executing ? <CircularProgress size={20} /> : "Run Report"}
                </Button>
              </Box>
            </Stack>
          </form>
        </Paper>
      ) : (
        <Box>
          <Button
            variant="contained"
            disabled={executing}
            onClick={() =>
              void handleExecute({
                preventDefault: () => {},
              } as FormEvent<HTMLFormElement>)
            }
          >
            {executing ? <CircularProgress size={20} /> : "Run Report"}
          </Button>
        </Box>
      )}

      {execError !== null ? <Alert severity="error">{execError}</Alert> : null}

      {result !== null ? (
        result.row_count === 0 ? (
          <Alert severity="info">Report returned no rows.</Alert>
        ) : (
          <ReportResultsTable result={result} tableName={report.report_key} />
        )
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------

interface ExecuteParamFieldProps {
  name: string;
  label: string;
  paramType: ReportParamType;
  required: boolean;
  value: string;
  onChange: (value: string) => void;
}

function ExecuteParamField({
  name,
  label,
  paramType,
  required,
  value,
  onChange,
}: ExecuteParamFieldProps): ReactElement {
  if (paramType === "boolean") {
    return (
      <TextField
        select
        label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        size="small"
        sx={{ maxWidth: 200 }}
      >
        <MenuItem value="true">True</MenuItem>
        <MenuItem value="false">False</MenuItem>
      </TextField>
    );
  }

  return (
    <TextField
      label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      required={required}
      size="small"
      type={paramType === "integer" || paramType === "float" ? "number" : "text"}
      placeholder={`${name} (${paramType})`}
      fullWidth
      slotProps={
        paramType === "float" ? { htmlInput: { step: "any" } } : undefined
      }
    />
  );
}
