"use client";

import type { ReactElement } from "react";

import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import CloseIcon from "@mui/icons-material/Close";

import PersonFocusedGraph from "@/components/PersonFocusedGraph";

interface PersonGraphDialogProps {
  open: boolean;
  personId?: string;
  elementId?: string;
  title: string;
  onClose: () => void;
}

export default function PersonGraphDialog({
  open,
  personId,
  elementId,
  title,
  onClose,
}: PersonGraphDialogProps): ReactElement {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="lg"
      fullWidth
      slotProps={{
        paper: {
          sx: {
            bgcolor: "var(--bg-card)",
            color: "var(--text-primary)",
            border: "1px solid var(--border)",
            borderRadius: "10px",
          },
        },
      }}
    >
      <DialogTitle sx={{ py: 1.5, borderBottom: "1px solid var(--border)", color: "var(--text-primary)" }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
          <span>Graph</span>
          <IconButton size="small" onClick={onClose} sx={{ color: "var(--text-muted)" }}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      </DialogTitle>
      <DialogContent
        dividers
        sx={{
          height: "75vh",
          display: "flex",
          flexDirection: "column",
          p: 2,
          bgcolor: "var(--bg-card)",
          color: "var(--text-primary)",
          borderColor: "var(--border)",
        }}
      >
        {open ? (
          <PersonFocusedGraph
            initialPersonId={personId}
            initialElementId={elementId}
            initialTitle={title}
            height="100%"
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}