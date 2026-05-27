"use client";

import { createTheme, type Theme } from "@mui/material/styles";

export const darkTheme: Theme = createTheme({
  cssVariables: true,
  palette: {
    mode: "dark",
    primary: { main: "#3b82f6", light: "#60a5fa", dark: "#1d4ed8" },
    secondary: { main: "#7c3aed" },
    background: { default: "#0f172a", paper: "#1e293b" },
    divider: "rgba(148,163,184,0.1)",
  },
  shape: { borderRadius: 6 },
  typography: {
    fontFamily: '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    htmlFontSize: 16,
    fontSize: 13,
    h4: { fontSize: "1.5rem", fontWeight: 600 },
    h5: { fontSize: "1.25rem", fontWeight: 600 },
    h6: { fontSize: "1.05rem", fontWeight: 600 },
    subtitle1: { fontSize: "0.9rem", fontWeight: 600 },
    body1: { fontSize: "0.85rem" },
    body2: { fontSize: "0.8rem" },
    caption: { fontSize: "0.72rem" },
    button: { textTransform: "none", fontWeight: 500 },
  },
  components: {
    MuiTable: { defaultProps: { size: "small" } },
    MuiTableCell: {
      styleOverrides: {
        root: { paddingTop: 6, paddingBottom: 6, paddingLeft: 10, paddingRight: 10 },
        head: { fontWeight: 600, backgroundColor: "rgba(0,0,0,0.15)" },
      },
    },
    MuiPaper: {
      defaultProps: { elevation: 0 },
      styleOverrides: { outlined: { borderColor: "rgba(148,163,184,0.12)" } },
    },
    MuiChip: { defaultProps: { size: "small" } },
    MuiTextField: { defaultProps: { size: "small" } },
    MuiButton: { defaultProps: { size: "small", disableElevation: true } },
    MuiIconButton: { defaultProps: { size: "small" } },
    MuiSelect: { defaultProps: { size: "small" } },
    MuiTabs: { styleOverrides: { root: { minHeight: 36 } } },
    MuiTab: { styleOverrides: { root: { minHeight: 36, padding: "6px 12px" } } },
    MuiToolbar: { styleOverrides: { dense: { minHeight: 44 } } },
    MuiDivider: { styleOverrides: { root: { borderColor: "rgba(148,163,184,0.08)" } } },
    MuiCard: {
      defaultProps: { elevation: 0 },
      styleOverrides: { root: { border: "1px solid rgba(148,163,184,0.12)" } },
    },
  },
});

export const lightTheme: Theme = createTheme({
  cssVariables: true,
  palette: {
    mode: "light",
    primary: { main: "#1d4ed8", light: "#3b82f6", dark: "#1e3a8a" },
    secondary: { main: "#7c3aed" },
    background: { default: "#f8fafc", paper: "#ffffff" },
    divider: "rgba(15,23,42,0.1)",
  },
  shape: { borderRadius: 6 },
  typography: {
    fontFamily: '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    htmlFontSize: 16,
    fontSize: 13,
    h4: { fontSize: "1.5rem", fontWeight: 600 },
    h5: { fontSize: "1.25rem", fontWeight: 600 },
    h6: { fontSize: "1.05rem", fontWeight: 600 },
    subtitle1: { fontSize: "0.9rem", fontWeight: 600 },
    body1: { fontSize: "0.85rem" },
    body2: { fontSize: "0.8rem" },
    caption: { fontSize: "0.72rem" },
    button: { textTransform: "none", fontWeight: 500 },
  },
  components: {
    MuiTable: { defaultProps: { size: "small" } },
    MuiTableCell: {
      styleOverrides: {
        root: { paddingTop: 6, paddingBottom: 6, paddingLeft: 10, paddingRight: 10 },
        head: { fontWeight: 600, backgroundColor: "rgba(15,23,42,0.03)" },
      },
    },
    MuiPaper: {
      defaultProps: { elevation: 0 },
      styleOverrides: { outlined: { borderColor: "rgba(15,23,42,0.12)" } },
    },
    MuiChip: { defaultProps: { size: "small" } },
    MuiTextField: { defaultProps: { size: "small" } },
    MuiButton: { defaultProps: { size: "small", disableElevation: true } },
    MuiIconButton: { defaultProps: { size: "small" } },
    MuiSelect: { defaultProps: { size: "small" } },
    MuiTabs: { styleOverrides: { root: { minHeight: 36 } } },
    MuiTab: { styleOverrides: { root: { minHeight: 36, padding: "6px 12px" } } },
    MuiToolbar: { styleOverrides: { dense: { minHeight: 44 } } },
    MuiDivider: { styleOverrides: { root: { borderColor: "rgba(15,23,42,0.08)" } } },
    MuiCard: {
      defaultProps: { elevation: 0 },
      styleOverrides: { root: { border: "1px solid rgba(15,23,42,0.12)" } },
    },
  },
});
