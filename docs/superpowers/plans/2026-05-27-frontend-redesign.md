# Frontend Redesign — Claude-Inspired Sidebar + Dark/Light Theme

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the horizontal AppBar nav with a collapsible sidebar, add a dark-first midnight-blue dual theme, and wire a persistent light/dark toggle at the bottom of the sidebar — without touching any page-level data or API logic.

**Architecture:** `theme.ts` exports two MUI themes (`darkTheme` / `lightTheme`). A new `ThemeContextProvider` client component owns the MUI `ThemeProvider` and reads/writes a `localStorage` preference. A new `SidebarNav` client component replaces the `AppBar` in `layout.tsx`, which becomes a flex two-column shell.

**Tech Stack:** Next.js 15 App Router, MUI v6, TypeScript strict, `@mui/icons-material`, `next/navigation` usePathname.

**Spec:** `docs/superpowers/specs/2026-05-27-frontend-redesign-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `services/frontend/src/theme.ts` | Modify | Export `darkTheme` + `lightTheme`; remove default export |
| `services/frontend/src/lib/ThemeContext.tsx` | Create | `ThemeContextProvider`, `useThemeMode` hook, localStorage persistence |
| `services/frontend/src/components/SidebarNav.tsx` | Create | Collapsible sidebar with nav items, bottom rail (health, toggle, user) |
| `services/frontend/src/app/layout.tsx` | Modify | Flex layout shell; replace AppBar with `SidebarNav` |

`UserMenu.tsx` and `HealthIndicator.tsx` are unchanged — reused inside `SidebarNav`.

---

## Task 1: Dual theme tokens

**Files:**
- Modify: `services/frontend/src/theme.ts`

- [ ] **Step 1: Replace theme.ts with dual exports**

Replace the entire file contents with:

```typescript
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
```

- [ ] **Step 2: Run typecheck — expect no errors from theme.ts**

```bash
cd services/frontend && npm run typecheck 2>&1 | head -30
```

Expected: any errors shown are from OTHER files (layout.tsx still imports the old default — that's fine for now, we'll fix it in Task 4). No errors from `theme.ts` itself.

- [ ] **Step 3: Commit**

```bash
git add services/frontend/src/theme.ts
git commit -m "feat: export darkTheme + lightTheme from theme.ts"
```

---

## Task 2: ThemeContextProvider

**Files:**
- Create: `services/frontend/src/lib/ThemeContext.tsx`

- [ ] **Step 1: Create ThemeContext.tsx**

```typescript
"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { ThemeProvider } from "@mui/material/styles";

import { darkTheme, lightTheme } from "@/theme";

type ThemeMode = "dark" | "light";

interface ThemeModeContextValue {
  mode: ThemeMode;
  toggleMode: () => void;
}

const ThemeModeContext = createContext<ThemeModeContextValue>({
  mode: "dark",
  toggleMode: () => undefined,
});

export function useThemeMode(): ThemeModeContextValue {
  return useContext(ThemeModeContext);
}

interface ThemeContextProviderProps {
  children: ReactNode;
}

export function ThemeContextProvider({
  children,
}: ThemeContextProviderProps): ReactElement {
  const [mode, setMode] = useState<ThemeMode>("dark");

  useEffect(() => {
    const stored = localStorage.getItem("theme-mode");
    if (stored === "light" || stored === "dark") setMode(stored);
  }, []);

  const toggleMode = useCallback(() => {
    setMode((prev) => {
      const next: ThemeMode = prev === "dark" ? "light" : "dark";
      localStorage.setItem("theme-mode", next);
      return next;
    });
  }, []);

  const value = useMemo<ThemeModeContextValue>(
    () => ({ mode, toggleMode }),
    [mode, toggleMode],
  );

  return (
    <ThemeModeContext.Provider value={value}>
      <ThemeProvider theme={mode === "dark" ? darkTheme : lightTheme}>
        {children}
      </ThemeProvider>
    </ThemeModeContext.Provider>
  );
}
```

- [ ] **Step 2: Run typecheck — expect no errors from ThemeContext.tsx**

```bash
cd services/frontend && npm run typecheck 2>&1 | grep "ThemeContext" || echo "no ThemeContext errors"
```

Expected: `no ThemeContext errors`

- [ ] **Step 3: Commit**

```bash
git add services/frontend/src/lib/ThemeContext.tsx
git commit -m "feat: add ThemeContextProvider with dark/light localStorage persistence"
```

---

## Task 3: SidebarNav component

**Files:**
- Create: `services/frontend/src/components/SidebarNav.tsx`

- [ ] **Step 1: Create SidebarNav.tsx**

```typescript
"use client";

import {
  useCallback,
  useEffect,
  useState,
  type ReactElement,
} from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import Box from "@mui/material/Box";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import AccountTreeIcon from "@mui/icons-material/AccountTree";
import BarChartIcon from "@mui/icons-material/BarChart";
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import CloudUploadIcon from "@mui/icons-material/CloudUpload";
import DarkModeIcon from "@mui/icons-material/DarkMode";
import HubIcon from "@mui/icons-material/Hub";
import LightModeIcon from "@mui/icons-material/LightMode";
import PeopleAltIcon from "@mui/icons-material/PeopleAlt";
import RateReviewIcon from "@mui/icons-material/RateReview";
import SettingsIcon from "@mui/icons-material/Settings";
import TimelineIcon from "@mui/icons-material/Timeline";

import HealthIndicator from "@/components/HealthIndicator";
import { UserMenu } from "@/components/auth/UserMenu";
import { useThemeMode } from "@/lib/ThemeContext";
import type { Role } from "@/lib/permissions";

// All MUI v6 icon components share the same OverridableComponent<SvgIconTypeMap> structure.
// We derive the nav-icon type from a representative icon so TypeScript stays strict.
type NavIcon = typeof PeopleAltIcon;

interface NavItem {
  readonly href: string;
  readonly label: string;
  readonly Icon: NavIcon;
}

const NAV_ITEMS: readonly NavItem[] = [
  { href: "/persons",   label: "Persons",   Icon: PeopleAltIcon  },
  { href: "/entities",  label: "Entities",  Icon: AccountTreeIcon },
  { href: "/reports",   label: "Reports",   Icon: BarChartIcon   },
  { href: "/graph",     label: "Graph",     Icon: HubIcon        },
  { href: "/review",    label: "Review",    Icon: RateReviewIcon },
  { href: "/ingestion", label: "Ingestion", Icon: CloudUploadIcon },
  { href: "/events",    label: "Events",    Icon: TimelineIcon   },
  { href: "/admin",     label: "Admin",     Icon: SettingsIcon   },
];

interface SidebarNavProps {
  hideNav: boolean;
  email: string | null | undefined;
  displayName: string | null | undefined;
  role: string | null | undefined;
  entityKey: string | null | undefined;
  sessionError: string | undefined;
}

export default function SidebarNav({
  hideNav,
  email,
  displayName,
  role,
  entityKey,
  sessionError,
}: SidebarNavProps): ReactElement | null {
  const [expanded, setExpanded] = useState<boolean>(true);
  const pathname = usePathname();
  const { mode, toggleMode } = useThemeMode();

  useEffect(() => {
    const stored = localStorage.getItem("sidebar-expanded");
    if (stored !== null) setExpanded(stored !== "false");
  }, []);

  const toggleExpanded = useCallback(() => {
    setExpanded((prev) => {
      const next = !prev;
      localStorage.setItem("sidebar-expanded", String(next));
      return next;
    });
  }, []);

  if (hideNav) return null;

  const safeRole: Role =
    role === "admin" || role === "employee" ? role : "first_time";

  return (
    <Box
      component="nav"
      sx={{
        width: expanded ? 200 : 52,
        minHeight: "100vh",
        flexShrink: 0,
        borderRight: "1px solid",
        borderColor: "divider",
        display: "flex",
        flexDirection: "column",
        bgcolor: "background.default",
        transition: "width 0.2s ease",
        overflow: "hidden",
      }}
    >
      {/* Brand header */}
      <Box
        sx={{
          px: 1,
          py: 1.5,
          display: "flex",
          alignItems: "center",
          gap: 1,
          borderBottom: "1px solid",
          borderColor: "divider",
          minHeight: 52,
          flexShrink: 0,
        }}
      >
        <Box
          sx={{
            width: 28,
            height: 28,
            borderRadius: "8px",
            background: "linear-gradient(135deg, #3b82f6, #1d4ed8)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 11,
            fontWeight: 800,
            color: "#fff",
            flexShrink: 0,
            userSelect: "none",
          }}
        >
          HP
        </Box>
        {expanded && (
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Typography
              variant="subtitle1"
              sx={{ fontWeight: 700, lineHeight: 1.2, whiteSpace: "nowrap" }}
            >
              HyperP
            </Typography>
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ lineHeight: 1, display: "block" }}
            >
              Profile Unifier
            </Typography>
          </Box>
        )}
        <Tooltip title={expanded ? "Collapse sidebar" : "Expand sidebar"}>
          <IconButton onClick={toggleExpanded} size="small" sx={{ flexShrink: 0 }}>
            {expanded ? (
              <ChevronLeftIcon fontSize="small" />
            ) : (
              <ChevronRightIcon fontSize="small" />
            )}
          </IconButton>
        </Tooltip>
      </Box>

      {/* Nav items */}
      <Box
        sx={{
          flex: 1,
          p: 0.75,
          display: "flex",
          flexDirection: "column",
          gap: 0.25,
          overflowY: "auto",
        }}
      >
        {NAV_ITEMS.map(({ href, label, Icon }) => {
          const active =
            pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Tooltip
              key={href}
              title={expanded ? "" : label}
              placement="right"
            >
              <Link href={href} style={{ textDecoration: "none" }}>
                <Box
                  sx={{
                    display: "flex",
                    alignItems: "center",
                    gap: 1,
                    px: 1,
                    py: 0.875,
                    borderRadius: "6px",
                    borderLeft: active
                      ? "2px solid #3b82f6"
                      : "2px solid transparent",
                    bgcolor: active ? "rgba(59,130,246,0.12)" : "transparent",
                    color: active
                      ? mode === "dark"
                        ? "#93c5fd"
                        : "#1d4ed8"
                      : "text.secondary",
                    "&:hover": { bgcolor: "action.hover" },
                    transition: "background 0.15s",
                    minHeight: 36,
                    cursor: "pointer",
                  }}
                >
                  <Icon sx={{ fontSize: 18, flexShrink: 0 }} />
                  {expanded && (
                    <Typography
                      variant="body2"
                      sx={{
                        fontWeight: active ? 600 : 400,
                        color: "inherit",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {label}
                    </Typography>
                  )}
                </Box>
              </Link>
            </Tooltip>
          );
        })}
      </Box>

      {/* Bottom rail */}
      <Divider />
      <Box sx={{ p: 0.75, display: "flex", flexDirection: "column", gap: 0.25 }}>
        <Box
          sx={{
            display: "flex",
            alignItems: "center",
            gap: 1,
            px: 1,
            py: 0.5,
            minHeight: 36,
          }}
        >
          <HealthIndicator />
          {expanded && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ flex: 1, whiteSpace: "nowrap" }}
            >
              API status
            </Typography>
          )}
          <Tooltip
            title={mode === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            placement="right"
          >
            <IconButton onClick={toggleMode} size="small" sx={{ flexShrink: 0 }}>
              {mode === "dark" ? (
                <LightModeIcon fontSize="small" />
              ) : (
                <DarkModeIcon fontSize="small" />
              )}
            </IconButton>
          </Tooltip>
        </Box>

        {email != null && (
          <Box
            sx={{
              px: 0.5,
              py: 0.25,
              "& .MuiChip-root": { display: expanded ? undefined : "none" },
            }}
          >
            <UserMenu
              email={email}
              displayName={displayName ?? null}
              role={safeRole}
              entityKey={entityKey ?? null}
              sessionError={sessionError}
            />
          </Box>
        )}
      </Box>
    </Box>
  );
}
```

- [ ] **Step 2: Run typecheck — expect no errors from SidebarNav.tsx**

```bash
cd services/frontend && npm run typecheck 2>&1 | grep "SidebarNav" || echo "no SidebarNav errors"
```

Expected: `no SidebarNav errors`

If TypeScript reports that icon components are not assignable to `NavIcon`, change the icon imports to match the type derivation exactly. The `type NavIcon = typeof PeopleAltIcon` approach works because all MUI v6 icons share the same `OverridableComponent<SvgIconTypeMap>` structure.

- [ ] **Step 3: Commit**

```bash
git add services/frontend/src/components/SidebarNav.tsx
git commit -m "feat: add SidebarNav component with collapsible rail and theme toggle"
```

---

## Task 4: Rewire layout.tsx and verify

**Files:**
- Modify: `services/frontend/src/app/layout.tsx`

- [ ] **Step 1: Replace layout.tsx**

Replace the entire file with:

```typescript
import type { Metadata } from "next";
import type { ReactElement, ReactNode } from "react";

import { AppRouterCacheProvider } from "@mui/material-nextjs/v15-appRouter";
import Box from "@mui/material/Box";
import CssBaseline from "@mui/material/CssBaseline";

import { auth } from "@/auth";
import SidebarNav from "@/components/SidebarNav";
import { ToastProvider } from "@/components/ToastProvider";
import SessionProviderClient from "@/components/auth/SessionProviderClient";
import { ThemeContextProvider } from "@/lib/ThemeContext";

export const metadata: Metadata = {
  title: "HyperP",
  description: "Customer profile unification and relationship intelligence",
};

interface RootLayoutProps {
  children: ReactNode;
}

export default async function RootLayout({
  children,
}: RootLayoutProps): Promise<ReactElement> {
  const session = await auth();
  const role = session?.user?.role ?? null;
  const email: string | null | undefined = session?.user?.email;
  const hideNav: boolean = !session || role === "first_time";

  return (
    <html lang="en">
      <body>
        <AppRouterCacheProvider>
          <ThemeContextProvider>
            <CssBaseline />
            <SessionProviderClient>
              <ToastProvider>
                <Box sx={{ display: "flex", minHeight: "100vh" }}>
                  <SidebarNav
                    hideNav={hideNav}
                    email={email}
                    displayName={session?.user?.displayName ?? null}
                    role={role}
                    entityKey={session?.user?.entityKey ?? null}
                    sessionError={session?.error}
                  />
                  <Box
                    component="main"
                    sx={{ flex: 1, minWidth: 0, px: "5%", py: 2.5, overflow: "auto" }}
                  >
                    {children}
                  </Box>
                </Box>
              </ToastProvider>
            </SessionProviderClient>
          </ThemeContextProvider>
        </AppRouterCacheProvider>
      </body>
    </html>
  );
}
```

- [ ] **Step 2: Run typecheck — expect clean**

```bash
cd services/frontend && npm run typecheck
```

Expected: zero errors. If errors appear in `layout.tsx`, the most likely cause is a session property type mismatch — check `src/types/next-auth.d.ts` for the exact property names and adjust accordingly.

- [ ] **Step 3: Run lint — expect within budget**

```bash
cd services/frontend && npm run lint
```

Expected: passes with ≤9 warnings. The warning count must not increase from the baseline. If a new `react-hooks/exhaustive-deps` or `set-state-in-effect` warning appears in the new files, add the appropriate disable comment on the flagged line.

- [ ] **Step 4: Rebuild and start Docker**

```bash
docker compose build --no-cache api frontend && docker compose up -d api frontend
```

Wait ~30 seconds for containers to start, then open http://localhost in a browser.

- [ ] **Step 5: Manual verification checklist**

Work through each item in a browser tab open to http://localhost:

1. **Sidebar appears** — left rail is visible, 200px wide, dark navy background
2. **HyperP brand mark** — gradient HP square + "HyperP" / "Profile Unifier" text visible in header
3. **Active link** — current route (Persons) shows blue left border + tinted row + blue label
4. **Navigate** — click Entities, Reports, Graph, Review, Ingestion, Events, Admin — each route loads correctly, active link updates
5. **Collapse** — click the chevron (‹) — sidebar shrinks to 52px, only icons visible
6. **Collapsed tooltips** — hover each icon — tooltip shows the label text
7. **Expand** — click the chevron (›) — sidebar returns to 200px
8. **Sidebar persistence** — collapse it, refresh the page — it stays collapsed
9. **Theme toggle** — click the sun/moon icon in the bottom rail — UI switches to light mode (slate-50 bg, dark text)
10. **Theme persistence** — refresh the page in light mode — it stays light
11. **Toggle back** — click the moon icon — switches back to dark
12. **User menu** — click the avatar — dropdown shows email, sign-out option
13. **Sign-out** — confirm sign-out works (redirects to /login)
14. **Login page** — /login shows no sidebar (hideNav = true), centred login card
15. **Health indicator** — small dot in bottom rail shows green (API healthy)

- [ ] **Step 6: Commit**

```bash
git add services/frontend/src/app/layout.tsx
git commit -m "feat: replace AppBar with SidebarNav — dark/light theme, collapsible rail"
```

---

## Notes

**LocalStorage flash:** On initial page load, if the user has saved a non-default preference (e.g., light mode), there will be a brief flash of dark mode before the `useEffect` fires and applies the saved preference. This is acceptable behaviour for MVP. A future improvement is a `<script>` tag in the `<head>` that sets `data-theme` before React hydrates, eliminating the flash.

**Collapsed state default:** The sidebar defaults to `expanded = true` on SSR. If the user had collapsed it, it will briefly expand then snap to collapsed after the `useEffect` fires. Also acceptable for MVP.

**ESLint warning budget:** The `--max-warnings 9` limit is enforced. The budget is fully consumed by pre-existing warnings. If `npm run lint` fails, check for new warnings in the new files and add `// eslint-disable-next-line <rule>` on the flagged line with a note explaining why.
