"use client";

import { useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";

import { statusColor, formatDob, formatDate } from "@/lib/display";
import type { FGNode, FGLink, SelectedItem } from "@/components/graph-utils";

interface DetailPanelProps {
  item: SelectedItem;
  onClose: () => void;
}

type GraphPropertyValue = string | number | boolean | null | readonly GraphPropertyValue[] | { readonly [key: string]: GraphPropertyValue };
type FormatKind = "date" | "datetime" | "dob" | "percent" | "mono";

interface FieldSpec {
  key: string;
  label: string;
  format?: FormatKind;
}

interface DisplayField extends FieldSpec {
  value: string;
  multiline: boolean;
  nestedRows: DisplayNestedRow[];
}

interface DisplayNestedRow {
  label: string;
  value: string;
  children: DisplayNestedRow[];
}

const PERSON_FIELDS: FieldSpec[] = [
  { key: "preferred_nric", label: "NRIC", format: "mono" },
  { key: "preferred_dob", label: "Date of birth", format: "dob" },
  { key: "preferred_phone", label: "Phone" },
  { key: "preferred_email", label: "Email" },
  { key: "preferred_address_normalized_full", label: "Address" },
  { key: "profile_completeness_score", label: "Profile completeness", format: "percent" },
  { key: "source_record_count", label: "Source records" },
  { key: "updated_at", label: "Updated", format: "datetime" },
];

const NODE_FIELD_OVERRIDES: Record<string, FieldSpec[]> = {
  Identifier: [
    { key: "identifier_type", label: "Type" },
    { key: "type", label: "Type" },
    { key: "value", label: "Value", format: "mono" },
    { key: "identifier_value", label: "Value", format: "mono" },
    { key: "normalized_value", label: "Normalized", format: "mono" },
  ],
  Address: [
    { key: "normalized_full", label: "Address" },
    { key: "full_address", label: "Address" },
    { key: "postal_code", label: "Postal code", format: "mono" },
    { key: "country_code", label: "Country" },
  ],
  SourceRecord: [
    { key: "source_system", label: "Source system" },
    { key: "source_record_pk", label: "Record key", format: "mono" },
    { key: "record_type", label: "Record type" },
    { key: "ingested_at", label: "Ingested", format: "datetime" },
  ],
  MatchDecision: [
    { key: "decision_id", label: "Decision ID", format: "mono" },
    { key: "decision", label: "Decision" },
    { key: "confidence", label: "Confidence", format: "percent" },
    { key: "created_at", label: "Created", format: "datetime" },
  ],
  ReviewCase: [
    { key: "review_case_id", label: "Case ID", format: "mono" },
    { key: "status", label: "Status" },
    { key: "priority", label: "Priority" },
    { key: "created_at", label: "Created", format: "datetime" },
    { key: "assigned_to", label: "Assignee" },
  ],
  Order: [
    { key: "order_id", label: "Order ID", format: "mono" },
    { key: "order_date", label: "Order date", format: "date" },
    { key: "total_amount", label: "Total" },
    { key: "status", label: "Status" },
  ],
};

const INTERNAL_KEYS = new Set([
  "embedding",
  "raw_payload",
  "raw_json",
  "payload",
  "properties",
  "source_hash",
]);

function formatKey(key: string): string {
  return key
    .replace(/^preferred_/, "")
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function parseListLikeText(value: string): GraphPropertyValue | null {
  const trimmed = value.trim();
  if (!(trimmed.startsWith("[") && trimmed.endsWith("]"))) return null;
  const content = trimmed.slice(1, -1).trim();
  if (!content) return [];
  return content
    .split(/,(?=(?:[^']*'[^']*')*[^']*$)/)
    .map((item) => item.trim().replace(/^['\"]|['\"]$/g, ""))
    .filter(Boolean);
}

function parseJsonLikeString(value: string): GraphPropertyValue | null {
  const trimmed = value.trim();
  if (!((trimmed.startsWith("[") && trimmed.endsWith("]")) || (trimmed.startsWith("{") && trimmed.endsWith("}")))) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed === null || typeof parsed === "string" || typeof parsed === "number" || typeof parsed === "boolean") return parsed;
    if (Array.isArray(parsed)) return parsed.map((item) => parseUnknownValue(item));
    if (typeof parsed === "object") {
      return Object.fromEntries(
        Object.entries(parsed).map(([key, item]) => [key, parseUnknownValue(item)]),
      );
    }
  } catch {
    return parseListLikeText(trimmed);
  }
  return null;
}

function parseUnknownValue(value: unknown): GraphPropertyValue {
  if (value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.map((item) => parseUnknownValue(item));
  if (typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, parseUnknownValue(item)]),
    );
  }
  return String(value);
}

function formatText(value: string): string {
  return value
    .replace(/&lt;br\s*\/??&gt;/gi, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replaceAll("_", " ")
    .replace(/[^\S\n]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function isRenderableValue(value: GraphPropertyValue): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") {
    const parsed = parseJsonLikeString(value);
    if (parsed !== null) return isRenderableValue(parsed);
    return value.trim().length > 0;
  }
  if (Array.isArray(value)) return value.some((item) => isRenderableValue(item));
  if (typeof value === "object" && value !== null) {
    return Object.values(value).some((item) => isRenderableValue(item));
  }
  return true;
}

function looksLikeTimestampKey(key: string): boolean {
  const lowered = key.toLowerCase();
  return lowered.endsWith("_at") || lowered.includes("timestamp") || lowered.endsWith("_time");
}

function looksLikeDateKey(key: string): boolean {
  const lowered = key.toLowerCase();
  return lowered.endsWith("_date") || lowered === "date";
}

function inferFieldFormat(key: string, format?: FormatKind): FormatKind | undefined {
  if (format) return format;
  if (looksLikeTimestampKey(key)) return "datetime";
  if (looksLikeDateKey(key)) return "date";
  return undefined;
}

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return formatText(value);
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function isGraphObjectValue(value: GraphPropertyValue): value is { readonly [key: string]: GraphPropertyValue } {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatObjectValue(value: { readonly [key: string]: GraphPropertyValue }): string {
  return Object.entries(value)
    .filter(([, item]) => isRenderableValue(item))
    .map(([key, item]) => `${formatKey(key)}: ${formatValue(item)}`)
    .join("\n");
}

function formatValue(value: GraphPropertyValue, format?: FormatKind): string {
  if (!isRenderableValue(value)) return "None";
  if (Array.isArray(value)) {
    return value.map((item) => formatValue(item)).filter((item) => item !== "None").join(", ") || "None";
  }
  if (isGraphObjectValue(value)) {
    return formatObjectValue(value) || "None";
  }
  if (format === "percent" && typeof value === "number") return value <= 1 ? `${Math.round(value * 100)}%` : `${Math.round(value)}%`;
  if (format === "dob" && typeof value === "string") return formatDob(value);
  if (format === "datetime" && typeof value === "string") return formatDateTime(value);
  if (format === "date" && typeof value === "string") return formatDate(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") {
    const parsed = parseJsonLikeString(value);
    if (parsed !== null) return formatValue(parsed, format);
    return formatText(value);
  }
  return String(value);
}

function orderedFields(props: Record<string, GraphPropertyValue>, specs: FieldSpec[]): FieldSpec[] {
  const used = new Set(specs.map((field) => field.key));
  const preferred = specs.filter((field) => isRenderableValue(props[field.key] ?? null));
  const remaining = Object.keys(props)
    .filter((key) => !used.has(key) && !INTERNAL_KEYS.has(key) && isRenderableValue(props[key] ?? null))
    .sort()
    .map((key) => ({ key, label: formatKey(key) }));
  return [...preferred, ...remaining].slice(0, 12);
}

function DetailShell({
  eyebrow,
  title,
  chip,
  onClose,
  children,
}: {
  eyebrow: string;
  title: string;
  chip: ReactElement;
  onClose: () => void;
  children: ReactElement;
}): ReactElement {
  const [expandedTitle, setExpandedTitle] = useState(false);

  return (
    <Paper
      elevation={8}
      sx={{
        position: "absolute",
        top: 14,
        right: 14,
        width: 400,
        maxHeight: "calc(100% - 28px)",
        overflowY: "auto",
        zIndex: 20,
        bgcolor: "color-mix(in srgb, var(--bg-card) 96%, transparent)",
        color: "var(--text-primary)",
        border: "1px solid var(--border)",
        borderRadius: "12px",
        backdropFilter: "blur(10px)",
        boxShadow: "0 18px 48px color-mix(in srgb, var(--bg-app) 62%, transparent)",
      }}
    >
      <Box sx={{ p: 1.75, pb: 1.25 }}>
        <Stack direction="row" justifyContent="space-between" alignItems="flex-start" spacing={1.5}>
          <Stack spacing={0.75} sx={{ minWidth: 0 }}>
            <Stack direction="row" spacing={0.75} alignItems="center" sx={{ minWidth: 0, flexWrap: "nowrap" }}>
              {chip}
              <Typography
                variant="caption"
                noWrap
                sx={{ color: "var(--text-muted)", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase", minWidth: 0 }}
              >
                {eyebrow}
              </Typography>
            </Stack>
            <Tooltip title={title}>
              <Typography
                variant="subtitle1"
                fontWeight={700}
                onClick={() => setExpandedTitle((current) => !current)}
                sx={{
                  color: "var(--text-primary)",
                  cursor: "pointer",
                  fontSize: 16,
                  letterSpacing: "-0.01em",
                  lineHeight: 1.25,
                  overflow: expandedTitle ? "visible" : "hidden",
                  textOverflow: expandedTitle ? "clip" : "ellipsis",
                  whiteSpace: expandedTitle ? "normal" : "nowrap",
                }}
              >
                {title}
              </Typography>
            </Tooltip>
          </Stack>
          <IconButton size="small" onClick={onClose} sx={{ color: "var(--text-muted)" }}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      </Box>
      <Divider sx={{ borderColor: "var(--border)" }} />
      <Box sx={{ p: 1.75 }}>{children}</Box>
    </Paper>
  );
}

function isMultilineText(value: string): boolean {
  return formatText(value).includes("\n");
}

function isComplexValue(value: GraphPropertyValue): boolean {
  if (Array.isArray(value)) return value.length > 1 || value.some((item) => typeof item === "object" && item !== null);
  if (typeof value === "object" && value !== null) return true;
  if (typeof value === "string") {
    const parsed = parseJsonLikeString(value);
    return parsed !== null && isComplexValue(parsed);
  }
  return false;
}

function rowsFromLabelText(value: string): DisplayNestedRow[] {
  const cleaned = formatText(value);
  const rows: DisplayNestedRow[] = [];
  for (const line of cleaned.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const separator = trimmed.indexOf(":");
    if (separator <= 0) continue;
    const label = trimmed.slice(0, separator).trim();
    const rowValue = trimmed.slice(separator + 1).trim();
    if (!label || !rowValue || label.length > 64) continue;
    rows.push({ label: formatText(label), value: formatText(rowValue), children: [] });
  }
  return rows.length >= 2 ? rows : [];
}

function normalizeNumberedRows(rows: DisplayNestedRow[]): DisplayNestedRow[] {
  const hasNumberedRows = rows.some((row) => /^\d+\./.test(row.label));
  if (!hasNumberedRows) return rows;
  return rows.map((row, index) => ({
    ...row,
    label: /^\d+\./.test(row.label) ? row.label : `${index + 1}. ${row.label}`,
  }));
}

function rowsFromValue(value: GraphPropertyValue): DisplayNestedRow[] {
  const parsed = typeof value === "string" ? parseJsonLikeString(value) : value;
  if (typeof value === "string" && parsed === null) {
    const labelRows = rowsFromLabelText(value);
    if (labelRows.length > 0) return normalizeNumberedRows(labelRows);
  }
  if (Array.isArray(parsed)) {
    return normalizeNumberedRows(parsed
      .map((item, index) => {
        const children = rowsFromValue(item);
        return {
          label: `Item ${index + 1}`,
          value: children.length > 0 ? "" : formatValue(item),
          children,
        };
      })
      .filter((row) => row.children.length > 0 || row.value !== "None"));
  }
  if (!isGraphObjectValue(parsed)) return [];
  return normalizeNumberedRows(Object.entries(parsed)
    .filter(([, item]) => isRenderableValue(item))
    .map(([key, item]) => {
      const children = rowsFromValue(item);
      return {
        label: formatKey(key),
        value: children.length > 0 ? "" : formatValue(item),
        children,
      };
    }));
}

function nestedRowsForValue(value: GraphPropertyValue): DisplayNestedRow[] {
  return rowsFromValue(value);
}

function displayFields(fields: FieldSpec[], props: Record<string, GraphPropertyValue>): DisplayField[] {
  return fields.map((field) => {
    const raw = props[field.key] ?? null;
    const nestedRows = nestedRowsForValue(raw);
    const format = inferFieldFormat(field.key, field.format);
    const value = formatValue(raw, format);
    return {
      ...field,
      value,
      multiline: nestedRows.length > 0 || isComplexValue(raw) || (typeof raw === "string" && isMultilineText(raw)),
      nestedRows,
    };
  });
}

function NestedRows({ rows, depth = 0 }: { rows: DisplayNestedRow[]; depth?: number }): ReactElement {
  return (
    <Stack spacing={0.6}>
      {rows.map((row) => (
        <Box key={`${row.label}-${row.value}`}>
          <Box sx={{ display: "grid", gridTemplateColumns: "118px minmax(0, 1fr)", columnGap: 1.5, alignItems: "baseline" }}>
            <Typography
              variant="caption"
              sx={{
                color: depth === 0 ? "var(--text-muted)" : "var(--text-faint)",
                fontSize: 11,
                fontWeight: 700,
                lineHeight: 1.45,
                pl: depth * 1.25,
              }}
            >
              {row.label}
            </Typography>
            {row.value ? (
              <Typography variant="body2" sx={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 500, lineHeight: 1.45, overflowWrap: "anywhere" }}>
                {row.value}
              </Typography>
            ) : <Box />}
          </Box>
          {row.children.length > 0 ? (
            <Box sx={{ mt: 0.45, pl: 1.5 }}>
              <NestedRows rows={row.children} depth={depth + 1} />
            </Box>
          ) : null}
        </Box>
      ))}
    </Stack>
  );
}

function FieldGrid({ fields, props }: { fields: FieldSpec[]; props: Record<string, GraphPropertyValue> }): ReactElement {
  if (fields.length === 0) {
    return <Typography variant="body2" sx={{ color: "var(--text-secondary)" }}>No displayable properties.</Typography>;
  }

  const renderedFields = displayFields(fields, props);

  return (
    <Stack
      component="dl"
      sx={{
        m: 0,
        borderTop: "1px solid var(--border-subtle)",
        "& > div": { borderBottom: "1px solid var(--border-subtle)" },
      }}
    >
      {renderedFields.map((field) => {
        const isNested = field.nestedRows.length > 0;
        return (
        <Box
          key={field.key}
          sx={{
            display: "grid",
            gridTemplateColumns: isNested ? "1fr" : "118px minmax(0, 1fr)",
            rowGap: isNested ? 0.75 : 0,
            columnGap: 1.5,
            py: 0.75,
            alignItems: field.multiline ? "flex-start" : "center",
          }}
        >
          <Typography
            component="dt"
            variant="caption"
            sx={{
              color: "var(--text-muted)",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.08em",
              lineHeight: 1.4,
              textTransform: "uppercase",
            }}
          >
            {field.label}
          </Typography>
          <Box component="dd" sx={{ m: 0, minWidth: 0 }}>
            {isNested ? (
              <NestedRows rows={field.nestedRows} />
            ) : (
              <Typography
                variant="body2"
                sx={{
                  color: "var(--text-primary)",
                  fontSize: 13,
                  fontFamily: inferFieldFormat(field.key, field.format) === "mono" ? "ui-monospace, SFMono-Regular, Menlo, monospace" : undefined,
                  fontWeight: 500,
                  whiteSpace: field.multiline ? "pre-line" : undefined,
                  lineHeight: field.multiline ? 1.5 : 1.35,
                  overflowWrap: "anywhere",
                }}
              >
                {field.value}
              </Typography>
            )}
          </Box>
        </Box>
        );
      })}
    </Stack>
  );
}

export default function GraphDetailPanel({ item, onClose }: DetailPanelProps): ReactElement {
  if (item.kind === "edge") {
    return <EdgeDetail edge={item.data} onClose={onClose} />;
  }
  if (item.data.label === "Person") {
    return <PersonDetail node={item.data} onClose={onClose} />;
  }
  return <NodeDetail node={item.data} onClose={onClose} />;
}

function PersonDetail({
  node,
  onClose,
}: {
  node: FGNode;
  onClose: () => void;
}): ReactElement {
  const props = node.properties;
  const status = String(props.status ?? "active");

  return (
    <DetailShell
      eyebrow="Person"
      title={formatText(node.displayName)}
      chip={<Chip label={status} size="small" color={statusColor(status)} variant="outlined" />}
      onClose={onClose}
    >
      <FieldGrid fields={orderedFields(props, PERSON_FIELDS)} props={props} />
    </DetailShell>
  );
}

function NodeDetail({
  node,
  onClose,
}: {
  node: FGNode;
  onClose: () => void;
}): ReactElement {
  const props = node.properties;
  const fields = orderedFields(props, NODE_FIELD_OVERRIDES[node.label] ?? []);

  return (
    <DetailShell
      eyebrow="Node detail"
      title={formatText(node.displayName)}
      chip={<Chip label={node.label} size="small" sx={{ bgcolor: node.color, color: "#fff", fontWeight: 700, maxWidth: 180, "& .MuiChip-label": { overflow: "hidden", textOverflow: "ellipsis" } }} />}
      onClose={onClose}
    >
      <FieldGrid fields={fields} props={props} />
    </DetailShell>
  );
}

function EdgeDetail({ edge, onClose }: { edge: FGLink; onClose: () => void }): ReactElement {
  const props = edge.properties ?? {};
  const fields = orderedFields(props, []);

  return (
    <DetailShell
      eyebrow="Relationship"
      title={formatText(edge.type)}
      chip={<Chip label="Edge" size="small" variant="outlined" sx={{ borderColor: "var(--border)", color: "var(--text-secondary)" }} />}
      onClose={onClose}
    >
      <FieldGrid fields={fields} props={props} />
    </DetailShell>
  );
}
