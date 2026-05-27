# Frontend Redesign — Claude-Inspired Blue Theme with Dark/Light Mode

**Date:** 2026-05-27
**Status:** Approved

## Overview

Restructure the HyperP frontend shell to a Claude-website-inspired aesthetic: collapsible sidebar navigation, dark-first midnight-blue theme, and a persistent light/dark toggle. All existing page functionality is preserved unchanged — only the layout shell and theme layer change.

---

## 1. Layout Shell

### Current state
`layout.tsx` renders a full-width `AppBar` with a horizontal `Toolbar` containing all 8 nav links. Pages are wrapped in a single `Container`.

### Target state
`layout.tsx` becomes a two-column flex layout:
- **Left column:** `SidebarNav` component (200px expanded / 52px collapsed)
- **Right column:** full-height scrollable `main` content area

```
┌─────────────────────────────────────────────────┐
│  SidebarNav (200px)  │  <main> (flex: 1)         │
│                      │                           │
│  [brand]             │  {children}               │
│  [nav items]         │                           │
│  ...                 │                           │
│  [health]            │                           │
│  [theme toggle]      │                           │
│  [user avatar]       │                           │
└─────────────────────────────────────────────────┘
```

The `AppBar` / `Toolbar` / horizontal `Button` nav is removed entirely. The `hideNav` logic (suppressing nav on login/pending/first_time) moves to `SidebarNav` — it renders `null` when `hideNav` is true, keeping the two-column layout intact but hiding the rail.

---

## 2. New Components

### `SidebarNav` (`src/components/SidebarNav.tsx`)
Client component (`"use client"`). Receives `role`, `email`, `displayName`, `entityKey`, `sessionError` as props (same data the current `UserMenu` receives from `layout.tsx`). Internally manages:
- `expanded: boolean` — persisted to `localStorage("sidebar-expanded")`, default `true`
- Theme mode toggle (delegates to `ThemeContext`)

**Anatomy:**
| Zone | Content |
|---|---|
| Brand header | 28px HP logo mark + "HyperP" wordmark + collapse `IconButton` |
| Nav items | 8 links — MUI icon + label text; active link detected via `usePathname()` |
| Bottom rail | Health dot (`HealthIndicator`), theme toggle (sun/moon `IconButton`), user avatar + email (expands to `UserMenu` dropdown) |

**Active link style:** `background: rgba(59,130,246,0.15)`, `borderLeft: "2px solid #3b82f6"`, label color `#93c5fd` (dark) / `#1d4ed8` (light).

**Collapsed state (52px):** Icons only. `MuiTooltip` wraps each nav item with the label as title. Brand area shows only the HP logo mark. Bottom rail shows health dot + sun/moon icon + avatar.

**Nav items and icons:**
| Route | Label | MUI Icon |
|---|---|---|
| `/persons` | Persons | `PeopleAlt` |
| `/entities` | Entities | `AccountTree` |
| `/reports` | Reports | `BarChart` |
| `/graph` | Graph | `Hub` |
| `/review` | Review | `RateReview` |
| `/ingestion` | Ingestion | `CloudUpload` |
| `/events` | Events | `Timeline` |
| `/admin` | Admin | `Settings` |

### `ThemeContext` (`src/lib/ThemeContext.tsx`)
Client-only context. Provides `mode: "dark" | "light"` and `toggleMode()`. Reads initial value from `localStorage("theme-mode")`, defaults to `"dark"`. Wraps the MUI `ThemeProvider` and switches between `darkTheme` and `lightTheme` exported from `src/theme.ts`.

---

## 3. Theme Tokens

`src/theme.ts` is refactored to export two named themes (`darkTheme`, `lightTheme`) instead of a single default export. Both share typography, shape, and component defaults — only palette differs.

### Dark theme palette (`darkTheme`)
| Token | Value |
|---|---|
| `background.default` | `#0f172a` (slate-950) |
| `background.paper` | `#1e293b` (slate-800) |
| `primary.main` | `#3b82f6` (blue-500) |
| `primary.light` | `#60a5fa` (blue-400) |
| `primary.dark` | `#1d4ed8` (blue-700) |
| `secondary.main` | `#7c3aed` (violet-600) |
| `text.primary` | `#e2e8f0` (slate-200) |
| `text.secondary` | `#64748b` (slate-500) |
| `divider` | `rgba(148,163,184,0.1)` |

### Light theme palette (`lightTheme`)
| Token | Value |
|---|---|
| `background.default` | `#f8fafc` (slate-50) |
| `background.paper` | `#ffffff` |
| `primary.main` | `#1d4ed8` (blue-700) |
| `primary.light` | `#3b82f6` (blue-500) |
| `primary.dark` | `#1e3a8a` (blue-900) |
| `secondary.main` | `#7c3aed` |
| `text.primary` | `#0f172a` (slate-950) |
| `text.secondary` | `#64748b` (slate-500) |
| `divider` | `rgba(15,23,42,0.1)` |

### Shared typography
No changes to font family (Inter), font sizes, or weight scale — existing values carry over to both themes.

### Shared component overrides
All existing component overrides (`MuiTable`, `MuiPaper`, `MuiChip`, etc.) carry forward. The `MuiTableCell` `head` background changes to `rgba(0,0,0,0.15)` in dark and `rgba(15,23,42,0.03)` in light (current value).

---

## 4. Wiring in `layout.tsx`

```tsx
// layout.tsx (simplified)
export default async function RootLayout({ children }) {
  const session = await auth();
  const hideNav = !session || session.user.role === "first_time";

  return (
    <html lang="en">
      <body>
        <AppRouterCacheProvider>
          <ThemeContextProvider>          {/* NEW — wraps ThemeProvider */}
            <CssBaseline />
            <SessionProviderClient>
              <ToastProvider>
                <Box sx={{ display: "flex", minHeight: "100vh" }}>
                  <SidebarNav                {/* NEW */}
                    hideNav={hideNav}
                    email={session?.user?.email}
                    displayName={session?.user?.displayName}
                    role={session?.user?.role}
                    entityKey={session?.user?.entityKey}
                    sessionError={session?.error}
                  />
                  <Box component="main" sx={{ flex: 1, minWidth: 0, p: "20px 5%" }}>
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

The `ThemeContextProvider` is a new client component that owns the `ThemeProvider` from MUI — this is necessary because `ThemeProvider` needs client context for the toggle. The async Server Component `RootLayout` remains a Server Component; only the new wrapper client components are client-side.

---

## 5. Files Changed

| File | Change |
|---|---|
| `src/theme.ts` | Export `darkTheme` + `lightTheme`; remove default export |
| `src/app/layout.tsx` | Replace `AppBar`/`Toolbar`/nav buttons with `SidebarNav` + flex layout |
| `src/components/SidebarNav.tsx` | **New** — full sidebar component |
| `src/lib/ThemeContext.tsx` | **New** — dark/light context + `ThemeContextProvider` |
| `src/components/auth/UserMenu.tsx` | No change — reused inside `SidebarNav` bottom rail |
| `src/components/HealthIndicator.tsx` | No change — reused inside `SidebarNav` bottom rail |

All pages (`persons/`, `review/`, `admin/`, etc.) are untouched — they inherit the updated theme tokens automatically via MUI's `sx` prop system.

---

## 6. Out of Scope

- Redesigning individual page layouts (tables, cards, filter panels) — purely cosmetic shell change
- Changing API contracts, BFF routes, or auth logic
- Adding animations or transitions beyond CSS `transition` on sidebar width
- Mobile/responsive breakpoints (sidebar collapses to hidden on mobile is a future task)

---

## 7. Testing Checklist

- [ ] Sidebar expands/collapses and persists state across page refresh
- [ ] Active nav item highlights correctly on each route
- [ ] Theme toggles dark ↔ light; preference persists across refresh
- [ ] `hideNav` suppresses sidebar on `/login` and `/pending`
- [ ] All 8 nav routes navigate correctly
- [ ] `UserMenu` sign-out still works from sidebar bottom rail
- [ ] `HealthIndicator` still polls and shows correct status
- [ ] TypeScript: `npm run typecheck` passes clean
- [ ] ESLint: `npm run lint` passes within warning budget
