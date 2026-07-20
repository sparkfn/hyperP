"use client";

import Dialog from "@mui/material/Dialog";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import React, { useId, useRef, type MouseEvent, type ReactElement, type ReactNode } from "react";

interface AccessibleDialogProps {
  children: ReactNode;
  frameless?: boolean;
  onClose: () => void;
  open: boolean;
  title: string;
  maxWidth?: "xs" | "sm" | "md" | "lg" | "xl";
  visuallyHideTitle?: boolean;
}

export default function AccessibleDialog({
  children,
  frameless = false,
  onClose,
  open,
  title,
  maxWidth = "sm",
  visuallyHideTitle = false,
}: AccessibleDialogProps): ReactElement {
  const titleId = useId();
  const gutterMouseDown = useRef(false);

  function handleContentMouseDown(event: MouseEvent<HTMLDivElement>): void {
    gutterMouseDown.current = event.target === event.currentTarget;
  }

  function handleContentClick(event: MouseEvent<HTMLDivElement>): void {
    const shouldClose = gutterMouseDown.current && event.target === event.currentTarget;
    gutterMouseDown.current = false;

    if (shouldClose) {
      onClose();
    }
  }

  return (
    <Dialog
      aria-labelledby={titleId}
      fullWidth
      maxWidth={maxWidth}
      onClose={onClose}
      open={open}
      slotProps={frameless ? {
        paper: {
          style: {
            background: "transparent",
            boxShadow: "none",
          },
        },
      } : undefined}
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
      <DialogContent
        onClick={frameless ? handleContentClick : undefined}
        onMouseDown={frameless ? handleContentMouseDown : undefined}
        style={frameless ? { display: "flex", justifyContent: "center", padding: 0 } : undefined}
      >
        {children}
      </DialogContent>
    </Dialog>
  );
}
