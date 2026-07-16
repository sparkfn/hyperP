"use client";

import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import React, { useId, type ReactElement, type ReactNode } from "react";

interface AccessibleDialogProps {
  children: ReactNode;
  onClose: () => void;
  open: boolean;
  title: string;
  maxWidth?: "xs" | "sm" | "md" | "lg" | "xl";
  visuallyHideTitle?: boolean;
}

export default function AccessibleDialog({
  children,
  onClose,
  open,
  title,
  maxWidth = "sm",
  visuallyHideTitle = false,
}: AccessibleDialogProps): ReactElement {
  const titleId = useId();

  return (
    <Dialog
      aria-labelledby={titleId}
      fullWidth
      maxWidth={maxWidth}
      onClose={onClose}
      open={open}
    >
      <DialogTitle
        id={titleId}
        sx={visuallyHideTitle ? {
          clip: "rect(0 0 0 0)",
          clipPath: "inset(50%)",
          height: 1,
          overflow: "hidden",
          position: "absolute",
          whiteSpace: "nowrap",
          width: 1,
        } : undefined}
      >
        {title}
      </DialogTitle>
      <DialogContent>{children}</DialogContent>
    </Dialog>
  );
}
