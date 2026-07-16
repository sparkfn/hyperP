"use client";

import Alert from "@mui/material/Alert";
import Snackbar, { type SnackbarCloseReason } from "@mui/material/Snackbar";
import React, { type ReactElement, type SyntheticEvent } from "react";

export interface ToastState {
  type: "success" | "error";
  message: string;
}

interface ActionToastProps extends ToastState {
  onDismiss: () => void;
}

export default function ActionToast({ type, message, onDismiss }: ActionToastProps): ReactElement {
  function handleClose(_event: Event | SyntheticEvent, reason?: SnackbarCloseReason): void {
    if (reason === "clickaway") return;
    onDismiss();
  }

  return (
    <Snackbar
      open
      autoHideDuration={6000}
      anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      onClose={handleClose}
    >
      <Alert
        closeText="Dismiss"
        onClose={handleClose}
        severity={type}
        variant="filled"
        sx={{ width: "100%" }}
      >
        {message}
      </Alert>
    </Snackbar>
  );
}
