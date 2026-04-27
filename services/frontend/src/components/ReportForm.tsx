"use client";

import { useState, type FormEvent, type ReactElement } from "react";

import Button from "@mui/material/Button";
import CircularProgress from "@mui/material/CircularProgress";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import AddIcon from "@mui/icons-material/Add";

import type { ReportDetail, ReportParamType } from "@/lib/api-types";
import ReportParamRow, { type EditableParam } from "@/components/ReportParamRow";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ReportFormPayload {
  report_key: string;
  display_name: string;
  description: string | null;
  category: string | null;
  cypher_query: string;
  parameters: {
    name: string;
    label: string;
    param_type: ReportParamType;
    required: boolean;
    default_value: string | null;
  }[];
}

interface ReportFormProps {
  mode: "create" | "edit";
  initialData?: ReportDetail;
  saving: boolean;
  onSave: (payload: ReportFormPayload) => void;
  onCancel: () => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let nextClientId = 0;
function genClientId(): string {
  nextClientId += 1;
  return `param_${nextClientId}`;
}

function toEditableParams(detail: ReportDetail): EditableParam[] {
  return detail.parameters.map((p) => ({
    clientId: genClientId(),
    name: p.name,
    label: p.label,
    param_type: p.param_type,
    required: p.required,
    default_value: p.default_value ?? "",
  }));
}

function emptyParam(): EditableParam {
  return {
    clientId: genClientId(),
    name: "",
    label: "",
    param_type: "string",
    required: false,
    default_value: "",
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function ReportForm({
  mode,
  initialData,
  saving,
  onSave,
  onCancel,
}: ReportFormProps): ReactElement {
  const [reportKey, setReportKey] = useState<string>(initialData?.report_key ?? "");
  const [displayName, setDisplayName] = useState<string>(initialData?.display_name ?? "");
  const [description, setDescription] = useState<string>(initialData?.description ?? "");
  const [category, setCategory] = useState<string>(initialData?.category ?? "");
  const [cypherQuery, setCypherQuery] = useState<string>(initialData?.cypher_query ?? "");
  const [params, setParams] = useState<EditableParam[]>(
    initialData ? toEditableParams(initialData) : [],
  );

  function handleAddParam(): void {
    setParams((prev) => [...prev, emptyParam()]);
  }

  function handleRemoveParam(clientId: string): void {
    setParams((prev) => prev.filter((p) => p.clientId !== clientId));
  }

  function updateParam(clientId: string, field: keyof EditableParam, value: string | boolean): void {
    setParams((prev) =>
      prev.map((p) => (p.clientId === clientId ? { ...p, [field]: value } : p)),
    );
  }

  function handleSubmit(e: FormEvent<HTMLFormElement>): void {
    e.preventDefault();
    onSave({
      report_key: reportKey.trim(),
      display_name: displayName.trim(),
      description: description.trim() || null,
      category: category.trim() || null,
      cypher_query: cypherQuery,
      parameters: params.map((p) => ({
        name: p.name.trim(),
        label: p.label.trim(),
        param_type: p.param_type,
        required: p.required,
        default_value: p.default_value.trim() || null,
      })),
    });
  }

  return (
    <form onSubmit={handleSubmit}>
      <Stack spacing={3}>
        <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
          <Typography variant="subtitle2" sx={{ mb: 2 }}>
            Report Details
          </Typography>
          <Stack spacing={2}>
            <TextField
              label="Report Key"
              value={reportKey}
              onChange={(e) => setReportKey(e.target.value)}
              required
              disabled={mode === "edit"}
              size="small"
              fullWidth
              placeholder="e.g. my_custom_report"
              helperText={
                mode === "create"
                  ? "Unique identifier. Cannot be changed after creation."
                  : undefined
              }
            />
            <TextField
              label="Display Name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
              size="small"
              fullWidth
              placeholder="e.g. My Custom Report"
            />
            <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
              <TextField
                label="Description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                size="small"
                fullWidth
                placeholder="Brief description of what this report shows"
              />
              <TextField
                label="Category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                size="small"
                sx={{ minWidth: 200 }}
                placeholder="e.g. analytics"
              />
            </Stack>
          </Stack>
        </Paper>

        <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
          <Typography variant="subtitle2" sx={{ mb: 2 }}>
            Cypher Query
          </Typography>
          <TextField
            value={cypherQuery}
            onChange={(e) => setCypherQuery(e.target.value)}
            required
            multiline
            minRows={6}
            maxRows={20}
            fullWidth
            placeholder={"MATCH (p:Person)\nRETURN p.person_id AS id, p.status AS status"}
            slotProps={{
              input: {
                sx: { fontFamily: "monospace", fontSize: "0.85rem" },
              },
            }}
          />
          <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
            Use $param_name syntax for parameters. They will be passed safely via Neo4j
            parameterized queries.
          </Typography>
        </Paper>

        <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
            <Typography variant="subtitle2">Parameters</Typography>
            <Button size="small" startIcon={<AddIcon />} onClick={handleAddParam}>
              Add Parameter
            </Button>
          </Stack>
          {params.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No parameters defined. Click &quot;Add Parameter&quot; to add one, or leave
              empty for parameterless reports.
            </Typography>
          ) : (
            <Stack spacing={2}>
              {params.map((p) => (
                <ReportParamRow
                  key={p.clientId}
                  param={p}
                  onChange={(field, value) => updateParam(p.clientId, field, value)}
                  onRemove={() => handleRemoveParam(p.clientId)}
                />
              ))}
            </Stack>
          )}
        </Paper>

        <Stack direction="row" spacing={2}>
          <Button type="submit" variant="contained" disabled={saving}>
            {saving ? (
              <CircularProgress size={20} />
            ) : mode === "create" ? (
              "Create Report"
            ) : (
              "Save Changes"
            )}
          </Button>
          <Button variant="outlined" onClick={onCancel} disabled={saving}>
            Cancel
          </Button>
        </Stack>
      </Stack>
    </form>
  );
}
