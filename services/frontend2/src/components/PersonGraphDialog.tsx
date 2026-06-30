"use client";

import type { ReactElement } from "react";

import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
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
      maxWidth="xl"
      fullWidth
      fullScreen={undefined}
      slotProps={{
        paper: {
          sx: {
            bgcolor: "var(--bg-card)",
            color: "var(--text-primary)",
            border: "1px solid var(--border)",
            borderRadius: { xs: "14px 14px 0 0", sm: "10px" },
            margin: { xs: 0, sm: "32px" },
            position: { xs: "fixed", sm: "relative" },
            bottom: { xs: 0, sm: "auto" },
            left: { xs: 0, sm: "auto" },
            right: { xs: 0, sm: "auto" },
            width: { xs: "100%", sm: "calc(100% - 64px)" },
            maxWidth: { xs: "100%", sm: "1536px" },
            maxHeight: { xs: "92dvh", sm: "calc(100% - 64px)" },
          },
        },
      }}
    >
      <DialogTitle sx={{ py: 1.25, borderBottom: "1px solid var(--border)", color: "var(--text-primary)" }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1.5}>
          <Stack spacing={0.25}>
            <Typography variant="h6" sx={{ fontSize: 20, fontWeight: 700, letterSpacing: "-0.02em" }}>
              Graph explorer
            </Typography>
          </Stack>
          <IconButton size="small" onClick={onClose} sx={{ color: "var(--text-muted)" }}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      </DialogTitle>
      <DialogContent
        dividers
        sx={{
          height: { xs: "calc(92dvh - 60px)", sm: "75vh" },
          display: "flex",
          flexDirection: "column",
          p: { xs: 0, sm: 2 },
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
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}