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
  const status = (props["status"] as string | null) ?? "active";
  const fullName = node.displayName;

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
        bgcolor: "var(--bg-card)",
        color: "var(--text-primary)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Stack direction="row" spacing={1} alignItems="center">
          <Chip
            label={status}
            size="small"
            color={statusColor(status)}
            variant="outlined"
          />
          <Typography variant="subtitle2" sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {fullName}
          </Typography>
        </Stack>
        <IconButton size="small" onClick={onClose} sx={{ color: "var(--text-muted)" }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <Divider sx={{ my: 1, borderColor: "var(--border)" }} />
      <Grid container size={12} spacing={0.5}>
        {PERSON_FIELDS.map(({ key, label, format }) => {
          const val = props[key];
          if (val === null || val === undefined) return null;
          return (
            <Grid key={key} size={6}>
              <Typography variant="caption" sx={{ color: "var(--text-muted)" }}>{label}</Typography>
              <Typography variant="body2" sx={{ fontFamily: format === "mono" ? "monospace" : undefined }}>
                {formatValue(val, format)}
              </Typography>
            </Grid>
          );
        })}
      </Grid>

      <Divider sx={{ my: 1, borderColor: "var(--border)" }} />
      <Link
        component="button"
        variant="body2"
        onClick={() => onOpenGraph(node.id, node.label, node.displayName)}
        sx={{ display: "flex", alignItems: "center", gap: 0.5, color: "var(--accent-text)" }}
      >
        Expand in graph <OpenInNewIcon sx={{ fontSize: 14 }} />
      </Link>
    </Paper>
  );
}

// ─── Generic non-Person node detail ────────────────────────────────────

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
        bgcolor: "var(--bg-card)",
        color: "var(--text-primary)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Stack direction="row" spacing={1} alignItems="center">
          <Chip label={node.label} size="small" />
          <Tooltip title={node.displayName}>
            <Typography variant="subtitle2" sx={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {node.displayName}
            </Typography>
          </Tooltip>
        </Stack>
        <IconButton size="small" onClick={onClose} sx={{ color: "var(--text-muted)" }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <Divider sx={{ my: 1, borderColor: "var(--border)" }} />
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
        {Object.keys(props).length === 0 && (
          <Typography variant="body2" sx={{ color: "var(--text-secondary)" }}>
            No properties
          </Typography>
        )}
      </Stack>
      <Divider sx={{ my: 1, borderColor: "var(--border)" }} />
      <Link
        component="button"
        variant="body2"
        onClick={() => onOpenGraph(node.id, node.label, node.displayName)}
        sx={{ display: "flex", alignItems: "center", gap: 0.5, color: "var(--accent-text)" }}
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
        bgcolor: "var(--bg-card)",
        color: "var(--text-primary)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
      }}
    >
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Chip label={edge.type} size="small" />
        <IconButton size="small" onClick={onClose} sx={{ color: "var(--text-muted)" }}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>
      {Object.keys(props).length > 0 && (
        <>
          <Divider sx={{ my: 1, borderColor: "var(--border)" }} />
          <Stack spacing={0.5}>
            {Object.entries(props).map(([key, val]) => (
              <Box key={key}>
                <Typography variant="caption" sx={{ color: "var(--text-muted)" }}>
                  {key}
                </Typography>
                <Typography variant="body2" sx={{ wordBreak: "break-all" }}>
                  {val === null ? "null" : String(val)}
                </Typography>
              </Box>
            ))}
          </Stack>
        </>
      )}
    </Paper>
  );
}