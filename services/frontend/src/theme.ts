"use client";

import { createTheme } from "@mui/material/styles";

const theme = createTheme({
  cssVariables: true,
  palette: {
    mode: "light",
    primary: { main: "#1f4e9e" },
    secondary: { main: "#7e57c2" },
    background: { default: "#f6f7f9", paper: "#ffffff" },
    divider: "rgba(15, 23, 42, 0.12)",
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
        head: { fontWeight: 600, backgroundColor: "rgba(15, 23, 42, 0.03)" },
      },
    },
    MuiPaper: {
      defaultProps: { elevation: 0 },
      styleOverrides: { outlined: { borderColor: "rgba(15, 23, 42, 0.12)" } },
    },
    MuiChip: { defaultProps: { size: "small" } },
    MuiTextField: { defaultProps: { size: "small" } },
    MuiButton: { defaultProps: { size: "small", disableElevation: true } },
    MuiIconButton: { defaultProps: { size: "small" } },
    MuiSelect: { defaultProps: { size: "small" } },
    MuiTabs: { styleOverrides: { root: { minHeight: 36 } } },
    MuiTab: { styleOverrides: { root: { minHeight: 36, padding: "6px 12px" } } },
    MuiToolbar: { styleOverrides: { dense: { minHeight: 44 } } },
    MuiDivider: { styleOverrides: { root: { borderColor: "rgba(15, 23, 42, 0.08)" } } },
    MuiCard: { defaultProps: { elevation: 0 }, styleOverrides: { root: { border: "1px solid rgba(15, 23, 42, 0.12)" } } },
  },
});

export default theme;
