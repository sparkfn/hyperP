"use client";

import {
  useCallback,
  useEffect,
  useState,
  type ReactElement,
} from "react";
import type { Route } from "next";
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
  readonly href: Route;
  readonly label: string;
  readonly Icon: NavIcon;
}

const NAV_ITEMS: readonly NavItem[] = [
  { href: "/persons"   as Route, label: "Persons",   Icon: PeopleAltIcon  },
  { href: "/entities"  as Route, label: "Entities",  Icon: AccountTreeIcon },
  { href: "/reports"   as Route, label: "Reports",   Icon: BarChartIcon   },
  { href: "/graph"     as Route, label: "Graph",     Icon: HubIcon        },
  { href: "/review"    as Route, label: "Review",    Icon: RateReviewIcon },
  { href: "/ingestion" as Route, label: "Ingestion", Icon: CloudUploadIcon },
  { href: "/events"    as Route, label: "Events",    Icon: TimelineIcon   },
  { href: "/admin"     as Route, label: "Admin",     Icon: SettingsIcon   },
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
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
                    borderLeft: "2px solid",
                    borderLeftColor: active ? "primary.main" : "transparent",
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
