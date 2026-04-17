"use client";

import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";

import type { SelectedItem } from "@/components/graph-utils";

interface DetailPanelProps {
  item: SelectedItem;
  onClose: () => void;
  onOpenGraph: (elementId: string, label: string, displayName: string) => void;
}

export default function GraphDetailPanel({ item, onClose, onOpenGraph }: DetailPanelProps): ReactElement {
  const title = item.kind === "node" ? item.data.label : item.data.type;
  const subtitle =
    item.kind === "node" ? item.data.displayName : `${item.data.type} relationship`;
  const props = item.data.properties;
  const isNode = item.kind === "node";

  return (
    <Paper
      elevation={4}
      sx={{
        position: "absolute",
        top: 16,
        right: 16,
        width: 340,
        maxHeight: "calc(100% - 32px)",
        overflow: "auto",
        zIndex: 10,
        p: 2,
      }}
    >
      <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
        <Chip
          label={title}
          size="small"
          sx={{ bgcolor: item.kind === "node" ? item.data.color : "#546e7a", color: "#fff" }}
        />
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>
      <Typography variant="subtitle2" sx={{ mt: 1 }}>
        {subtitle}
      </Typography>
      {isNode ? (
        <Typography
          variant="caption"
          color="primary"
          sx={{ cursor: "pointer", textDecoration: "underline", display: "block", mb: 1 }}
          onClick={() => onOpenGraph(item.data.id, item.data.label, item.data.displayName)}
        >
          Open graph from this node
        </Typography>
      ) : null}
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
    </Paper>
  );
}
