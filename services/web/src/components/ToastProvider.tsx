"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";

import Alert from "@mui/material/Alert";
import Snackbar from "@mui/material/Snackbar";

export type ToastSeverity = "success" | "error" | "info" | "warning";

interface ToastState {
  open: boolean;
  message: string;
  severity: ToastSeverity;
}

interface ToastContextValue {
  showToast: (message: string, severity?: ToastSeverity) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const INITIAL_STATE: ToastState = { open: false, message: "", severity: "info" };

export function ToastProvider({ children }: { children: ReactNode }): ReactElement {
  const [state, setState] = useState<ToastState>(INITIAL_STATE);

  const showToast = useCallback((message: string, severity: ToastSeverity = "info"): void => {
    setState({ open: true, message, severity });
  }, []);

  const handleClose = useCallback((): void => {
    setState((prev) => ({ ...prev, open: false }));
  }, []);

  const value: ToastContextValue = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <Snackbar
        open={state.open}
        autoHideDuration={5000}
        onClose={handleClose}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
      >
        <Alert
          onClose={handleClose}
          severity={state.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {state.message}
        </Alert>
      </Snackbar>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (ctx === null) {
    throw new Error("useToast must be used inside <ToastProvider>");
  }
  return ctx;
}
