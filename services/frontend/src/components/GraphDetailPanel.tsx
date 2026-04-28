"use client";

import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";

import { statusColor, formatDob, formatDate } from "@/lib/display";
import type { FGNode, FGLink, SelectedItem } from "@/components/graph-utils";

interface DetailPanelProps {
  item: SelectedItem;
  onClose: () => void;
  onOpenGraph: (elementId: string, label: string, displayName: string) => void;
}

type FormatKind = "date" | "dob" | "percent" | "mono";

/** Key fields to display for Person nodes, in order. */
const PERSON_FIELDS: Array<{ key: string; label: string; format?: FormatKind }> = [
  { key: "preferred_nric", label: "NRIC", format: "mono" },
  { key: "preferred_dob", label: "Date of Birth", format: "dob" },
  { key: "preferred_phone", label: "Phone" },
  { key: "preferred_email", label: "Email" },
  { key: "preferred_address_normalized_full", label: "Address" },
  { key: "profile_completeness_score", label: "Profile Completeness", format: "percent" },
  { key: "source_record_count", label: "Source Records" },
  { key: "updated_at", label: "Updated", format: "date" },
];

function formatValue(val: string | number | boolean | null, format?: FormatKind): string {
  if (val === null || val === undefined) return "—";
  if (format === "percent" && typeof val === "number") return `${(val * 100).toFixed(0)}%`;
  if (format === "dob" && typeof val === "string" && val) return formatDob(val);
  if (format === "date" && typeof val === "string" && val) return formatDate(val);
  if (format === "mono") return String(val);
  return String(val);
}

export default function GraphDetailPanel({ item, onClose, onOpenGraph }: DetailPanelProps): ReactElement {
  if (item.kind === "edge") {
    return <EdgeDetail edge={item.data} onClose={onClose} />;
  }
  if (item.data.label === "Person") {
    return <PersonDetail node={item.data} onClose={onClose} onOpenGraph={onOpenGraph} />;
  }
  return <NodeDetail node={item.data} onClose={onClose} onOpenGraph={onOpenGraph} />;
}

// ─── Person detail (rich, matches Person page header) ─────────────────

function PersonDetail({
  node,
  onClose,
  onOpenGraph,
}: {
  node: FGNode;
  onClose: () => void;
  onOpenGraph: (elementId: string, label: string, displayName: string) => void;
}): ReactElement {
  const props = node.properties;
  const status = (props["status"] as string | null) ?? "—";
  const isHighValue = props["is_high_value"] === true || props["is_high_value"] === "true";
  const isHighRisk = props["is_high_risk"] === true || props["is_high_risk"] === "true";
  const personId = (props["person_id"] as string | null) ?? node.id;

  return (
    <Paper
      elevation={4}
      sx={{
        position: "absolute",
        top: 16,
        right: 16,
        width: 380,
        maxHeight: "calc(100% - 32px)",
        overflowY: "auto",
        zIndex: 20,
        p: 2,
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="h6" fontWeight={600} sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {node.displayName}
        </Typography>
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1 }}>
        <Chip label={status} size="small" color={statusColor(status)} />
        {isHighValue ? <Chip label="high value" size="small" color="primary" /> : null}
        {isHighRisk ? <Chip label="high risk" size="small" color="error" /> : null}
      </Stack>

      <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: "block" }}>
        {personId}
      </Typography>

      <Divider sx={{ my: 1.5 }} />

      <Grid container spacing={1.5}>
        {PERSON_FIELDS.map(({ key, label, format }) => {
          const rawVal: string | number | boolean | null = key in props ? (props[key] ?? null) : null;
          return (
            <Grid key={key} size={{ xs: 12, sm: 6 }}>
              <Typography variant="caption" color="text.secondary" display="block">
                {label}
              </Typography>
              <Tooltip title={rawVal !== null ? String(rawVal) : ""} enterDelay={400}>
                <Typography
                  variant="body2"
                  fontFamily={format === "mono" ? "monospace" : undefined}
                  sx={{
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {formatValue(rawVal, format)}
                </Typography>
              </Tooltip>
            </Grid>
          );
        })}
      </Grid>

      <Divider sx={{ my: 1.5 }} />

      <Stack direction="row" spacing={2}>
        <Link
          component="button"
          variant="body2"
          onClick={() => onOpenGraph(node.id, node.label, node.displayName)}
          sx={{ display: "flex", alignItems: "center", gap: 0.5 }}
        >
          Expand in graph <OpenInNewIcon sx={{ fontSize: 14 }} />
        </Link>
        <Link
          href={`/persons/${encodeURIComponent(personId)}`}
          target="_blank"
          rel="noopener noreferrer"
          variant="body2"
          sx={{ display: "flex", alignItems: "center", gap: 0.5 }}
        >
          More <OpenInNewIcon sx={{ fontSize: 14 }} />
        </Link>
      </Stack>
    </Paper>
  );
}

// ─── Generic node detail (key-value) ──────────────────────────────────

function NodeDetail({
  node,
  onClose,
  onOpenGraph,
}: {
  node: FGNode;
  onClose: () => void;
  onOpenGraph: (elementId: string, label: string, displayName: string) => void;
}): ReactElement {
  const props = node.properties;

  return (
    <Paper
      elevation={4}
      sx={{
        position: "absolute",
        top: 16,
        right: 16,
        width: 340,
        maxHeight: "calc(100% - 32px)",
        overflowY: "auto",
        zIndex: 20,
        p: 2,
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Stack direction="row" spacing={1} alignItems="center">
          <Chip label={node.label} size="small" sx={{ fontSize: "0.7rem" }} />
          <Typography variant="subtitle2" sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {node.displayName}
          </Typography>
        </Stack>
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <Divider sx={{ my: 1 }} />
      <Stack spacing={0.5}>
        {Object.entries(props).map(([key, val]) => (
          <Box key={key}>
            <Typography variant="caption" color="text.secondary">
              {key}
            </Typography>
            <Typography variant="body2" sx={{ wordBreak: "break-all" }}>
              {val === null ? "null" : String(val)}
            </Typography>
          </Box>
        ))}
        {Object.keys(props).length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No properties
          </Typography>
        ) : null}
      </Stack>
      <Divider sx={{ my: 1 }} />
      <Link
        component="button"
        variant="body2"
        onClick={() => onOpenGraph(node.id, node.label, node.displayName)}
        sx={{ display: "flex", alignItems: "center", gap: 0.5 }}
      >
        Expand in graph <OpenInNewIcon sx={{ fontSize: 14 }} />
      </Link>
    </Paper>
  );
}

// ─── Edge detail ──────────────────────────────────────────────────────

function EdgeDetail({
  edge,
  onClose,
}: {
  edge: FGLink;
  onClose: () => void;
}): ReactElement {
  const props = edge.properties ?? {};

  return (
    <Paper
      elevation={4}
      sx={{
        position: "absolute",
        top: 16,
        right: 16,
        width: 340,
        maxHeight: "calc(100% - 32px)",
        overflowY: "auto",
        zIndex: 20,
        p: 2,
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Chip label={edge.type} size="small" />
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>
      {Object.keys(props).length > 0 ? (
        <>
          <Divider sx={{ my: 1 }} />
          <Stack spacing={0.5}>
            {Object.entries(props).map(([key, val]) => (
              <Box key={key}>
                <Typography variant="caption" color="text.secondary">
                  {key}
                </Typography>
                <Typography variant="body2" sx={{ wordBreak: "break-all" }}>
                  {val === null ? "null" : String(val)}
                </Typography>
              </Box>
            ))}
          </Stack>
        </>
      ) : null}
    </Paper>
  );
}