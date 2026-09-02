"use client";

import { useCallback, useEffect, useState, type ReactElement } from "react";

import Autocomplete from "@mui/material/Autocomplete";
import Skeleton from "@mui/material/Skeleton";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import HomeIcon from "@mui/icons-material/Home";
import OpenInFullIcon from "@mui/icons-material/OpenInFull";
import SearchIcon from "@mui/icons-material/Search";
import InputAdornment from "@mui/material/InputAdornment";

import { bffFetchEnvelope } from "@/lib/api-client";
import type { Person } from "@/lib/api-types";
import { avatarColor, completenessColor, statusHex } from "@/lib/display";
import PersonGraphViewer from "@/components/PersonGraphViewer";

interface NavEntry {
  elementId?: string;
  personId?: string;
  title: string;
}

interface PersonOption {
  personId: string;
  name: string;
  email: string | null;
  phone: string | null;
  status: string;
  completeness: number;
}

const SKELETON_ROW = (
  <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, px: 1.5, py: 0.75 }}>
    <Skeleton variant="circular" width={34} height={34} sx={{ flexShrink: 0 }} />
    <Box sx={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 0.5 }}>
      <Skeleton variant="text" width="55%" height={16} />
      <Skeleton variant="text" width="38%" height={13} />
    </Box>
    <Box sx={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 0.5, flexShrink: 0 }}>
      <Skeleton variant="text" width={36} height={13} />
      <Skeleton variant="text" width={28} height={13} />
    </Box>
  </Box>
);

const SKELETON_LOADING_TEXT = (
  <Box sx={{ py: 0.5 }}>
    {Array.from({ length: 2 }, (_, i) => (
      <Box key={i}>{SKELETON_ROW}</Box>
    ))}
  </Box>
);


interface NodeContextMenu {
  mouseX: number;
  mouseY: number;
  elementId: string;
  label: string;
  displayName: string;
  personId: string | null;
}

interface PersonFocusedGraphProps {
  initialPersonId?: string;
  initialElementId?: string;
  initialTitle: string;
  /** When true: full-canvas layout with overlay controls, no left panel. */
  overlayMode?: boolean;
  /** Hide breadcrumbs and graph overlay controls for embedded cards. */
  pureGraph?: boolean;
  /** Called when the user clicks the maximize button (only shown in overlayMode). */
  onMaximize?: () => void;
}

function openGraphTab(elementId: string, label: string, displayName: string): void {
  const params = new URLSearchParams({ element_id: elementId, label, name: displayName });
  window.open(`/graph?${params.toString()}`, "_blank");
}

export default function PersonFocusedGraph({
  initialPersonId,
  initialElementId,
  initialTitle,
  overlayMode = false,
  pureGraph = false,
  onMaximize,
}: PersonFocusedGraphProps): ReactElement {
  const [navStack, setNavStack] = useState<NavEntry[]>([
    { elementId: initialElementId, personId: initialPersonId, title: initialTitle },
  ]);
  const [contextMenu, setContextMenu] = useState<NodeContextMenu | null>(null);

  const current = navStack[navStack.length - 1] ?? { personId: initialPersonId, elementId: initialElementId, title: initialTitle };
  const canGoBack = navStack.length > 1;
  const isUnloaded = !current.elementId && !current.personId;

  // Search state — active only on the empty canvas
  const [searchInput, setSearchInput] = useState("");
  const [searchOptions, setSearchOptions] = useState<PersonOption[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);

  useEffect(() => {
    if (!isUnloaded) return;
    const q = searchInput.trim();
    if (q.length < 3) { setSearchOptions([]); return; }
    setSearchLoading(true);
    let cancelled = false;
    const controller = new AbortController();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void bffFetchEnvelope<Person[]>(`/bff/persons/search?q=${encodeURIComponent(q)}&limit=8`, {
      signal: controller.signal,
    })
      .then((res) => {
        if (!cancelled) {
          setSearchOptions((res.data ?? []).map((p) => ({
            personId: p.person_id,
            name: p.preferred_full_name ?? p.person_id,
            email: p.preferred_email,
            phone: p.preferred_phone,
            status: p.status,
            completeness: p.profile_completeness_score,
          })));
        }
      })
      .catch(() => { if (!cancelled) setSearchOptions([]); })
      .finally(() => { if (!cancelled) setSearchLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [searchInput, isUnloaded]);

  const handleNavigateNode = useCallback(
    (elementId: string, _label: string, displayName: string): void => {
      setNavStack((prev) => [...prev, { elementId, title: displayName }]);
    },
    [],
  );

  function handleBack(): void {
    setNavStack((prev) => (prev.length > 1 ? prev.slice(0, -1) : prev));
  }

  function handleReset(): void {
    setNavStack((prev) => (prev[0] ? [prev[0]] : prev));
  }

  function handleNewSearch(): void {
    setNavStack([{ title: "Graph Explorer" }]);
    setSearchInput("");
    setSearchOptions([]);
  }

  const handleNodeContextMenu = useCallback(
    (
      elementId: string,
      label: string,
      displayName: string,
      position: { x: number; y: number },
      properties: Record<string, string | number | boolean | null>,
    ): void => {
      const rawPersonId = properties["person_id"];
      const personId = typeof rawPersonId === "string" && rawPersonId.length > 0 ? rawPersonId : null;
      setContextMenu({ mouseX: position.x, mouseY: position.y, elementId, label, displayName, personId });
    },
    [],
  );

  function handleOpenInNewTab(): void {
    if (!contextMenu) return;
    openGraphTab(contextMenu.elementId, contextMenu.label, contextMenu.displayName);
    setContextMenu(null);
  }

  function handleOpenPersonPage(): void {
    if (!contextMenu?.personId) return;
    window.open(`/persons/${encodeURIComponent(contextMenu.personId)}`, "_blank");
    setContextMenu(null);
  }

  const btnSx = {
    bgcolor: "var(--bg-card)",
    border: "1px solid var(--border)",
    color: "var(--text-secondary)",
    "&:hover": { bgcolor: "var(--bg-surface-2)", color: "var(--text-primary)" },
  };

  const navOverlay = (!pureGraph && (canGoBack || (overlayMode && onMaximize))) ? (
    <Box sx={{ position: "absolute", top: 12, right: 12, zIndex: 10, display: "flex", gap: 1 }}>
      {canGoBack && (
        <Tooltip title="Back">
          <IconButton size="small" onClick={handleBack} sx={btnSx}>
            <ArrowBackIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
      {canGoBack && (
        <Tooltip title="Reset to initial graph">
          <IconButton size="small" onClick={handleReset} sx={btnSx}>
            <HomeIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
      {overlayMode && onMaximize && (
        <Tooltip title="Maximize graph">
          <IconButton size="small" onClick={onMaximize} sx={btnSx}>
            <OpenInFullIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
    </Box>
  ) : null;

  const itemSx = { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const, maxWidth: 120 };
  const linkCrumbSx = { ...itemSx, cursor: "pointer", color: "var(--text-secondary)", "&:hover": { color: "var(--text-primary)" }, transition: "color 0.12s", flexShrink: 1 };

  const breadcrumb = (
    <Box sx={{ display: "flex", alignItems: "center", gap: "8px", fontSize: 13, color: "var(--text-secondary)", overflow: "hidden" }}>
      <Box component="span" onClick={handleNewSearch} sx={{ ...linkCrumbSx, flexShrink: 0 }}>
        Graph Explorer
      </Box>
      {navStack.flatMap((entry, i) => {
        const isCurrent = i === navStack.length - 1;
        return [
          <Box key={`sep-${i}`} component="span" sx={{ color: "var(--text-muted)", flexShrink: 0 }}>/</Box>,
          isCurrent
            ? <Box key={`cur-${i}`} component="span" sx={{ ...itemSx, color: "var(--text-primary)", fontWeight: 500, flexShrink: 1 }}>{entry.title}</Box>
            : <Box key={`nav-${i}`} component="span" onClick={() => setNavStack(navStack.slice(0, i + 1))} sx={linkCrumbSx}>{entry.title}</Box>,
        ];
      })}
    </Box>
  );

  const searchAutocomplete = (
    <Autocomplete
      fullWidth
      options={searchOptions}
      getOptionLabel={(opt) => opt.name}
      getOptionKey={(opt) => opt.personId}
      loading={searchLoading}
      loadingText={SKELETON_LOADING_TEXT}
      filterOptions={(x) => x}
      inputValue={searchInput}
      onInputChange={(_, val) => setSearchInput(val)}
      onChange={(_, val) => {
        if (val) setNavStack([{ personId: val.personId, title: val.name }]);
      }}
      noOptionsText={searchInput.trim().length < 3 ? "Type at least 3 characters…" : "No results"}
      renderOption={(props, opt) => {
        const initials = opt.name.split(" ").filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
        const circ = 2 * Math.PI * 14;
        const dash = circ * opt.completeness;
        const gap = circ - dash;
        const ringCol = completenessColor(opt.completeness);
        const subtitle = opt.email ?? opt.phone ?? opt.personId;
        return (
          <li {...props} key={opt.personId}>
            <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, width: "100%", py: 0.25 }}>
              <Box sx={{ position: "relative", width: 34, height: 34, flexShrink: 0 }}>
                <svg width="34" height="34" viewBox="0 0 34 34" fill="none" style={{ position: "absolute", inset: 0 }}>
                  <circle cx="17" cy="17" r="14" stroke={ringCol} strokeWidth="1.5" opacity="0.2" />
                  <circle cx="17" cy="17" r="14" stroke={ringCol} strokeWidth="1.5" strokeLinecap="round"
                    strokeDasharray={`${dash} ${gap}`} transform="rotate(-90 17 17)" />
                </svg>
                <Box sx={{ position: "absolute", inset: "3px", borderRadius: "50%", bgcolor: avatarColor(opt.name), display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontSize: 11, fontWeight: 700 }}>
                  {initials}
                </Box>
              </Box>
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography sx={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", lineHeight: 1.35 }}>{opt.name}</Typography>
                <Typography sx={{ fontSize: 12, color: "var(--text-muted)", lineHeight: 1.3 }} noWrap>{subtitle}</Typography>
              </Box>
              <Box sx={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 0.25, flexShrink: 0 }}>
                <Typography sx={{ fontSize: 11, color: statusHex(opt.status), fontWeight: 600, textTransform: "capitalize" }}>{opt.status}</Typography>
                <Typography sx={{ fontSize: 11, color: ringCol, fontWeight: 600 }}>{Math.round(opt.completeness * 100)}%</Typography>
              </Box>
            </Box>
          </li>
        );
      }}
      slotProps={{
        paper: {
          sx: {
            mt: 1,
            bgcolor: "var(--bg-card)",
            border: "1px solid var(--border)",
            borderRadius: "16px",
            boxShadow: "0 8px 32px rgba(0,0,0,0.18)",
            "& .MuiAutocomplete-listbox": { p: 0.75 },
            "& .MuiAutocomplete-option": {
              borderRadius: "8px",
              color: "var(--text-primary)",
              fontSize: 14,
              px: 1.5,
              py: 1,
              '&[aria-selected="true"], &.Mui-focused, &:hover': {
                bgcolor: "var(--bg-surface-2)",
              },
            },
            "& .MuiAutocomplete-noOptions, & .MuiAutocomplete-loading": {
              color: "var(--text-muted)",
              fontSize: 13,
              py: 2,
              textAlign: "center",
            },
          },
        },
      }}
      renderInput={(params) => (
        <TextField
          {...params}
          placeholder="Search by name, email, phone…"
          fullWidth
          slotProps={{
            input: {
              ...params.InputProps,
              startAdornment: (
                <InputAdornment position="start" sx={{ ml: 0.5 }}>
                  <SearchIcon sx={{ color: "var(--text-muted)", fontSize: 20 }} />
                </InputAdornment>
              ),
              endAdornment: null,
              sx: {
                borderRadius: "16px",
                bgcolor: "var(--bg-card)",
                boxShadow: "0 2px 12px rgba(0,0,0,0.08)",
                "& fieldset": { borderColor: "var(--border)", borderWidth: 1 },
                "&:hover fieldset": { borderColor: "var(--border-strong)" },
                "&.Mui-focused fieldset": { borderColor: "var(--border-strong)", borderWidth: 1 },
                "& input": {
                  color: "var(--text-primary)",
                  fontSize: 15,
                  py: "11px",
                  px: 1,
                  "&::placeholder": { color: "var(--text-muted)", opacity: 1 },
                },
              },
            },
          }}
        />
      )}
    />
  );

  if (isUnloaded) {
    return (
      <Box sx={{ height: "100%", minHeight: 0, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-primary)", px: 2 }}>
        <Box sx={{ width: "100%", maxWidth: 480, display: "flex", flexDirection: "column", gap: 3 }}>
          <Box sx={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 1.5 }}>
            <svg width="48" height="48" viewBox="0 0 56 56" fill="none" style={{ color: "var(--text-secondary)", opacity: 0.5 }}>
              <circle cx="28" cy="28" r="7" fill="currentColor" />
              <circle cx="10" cy="14" r="5" fill="currentColor" opacity="0.7" />
              <circle cx="46" cy="14" r="5" fill="currentColor" opacity="0.7" />
              <circle cx="10" cy="42" r="4" fill="currentColor" opacity="0.5" />
              <circle cx="46" cy="42" r="4" fill="currentColor" opacity="0.5" />
              <line x1="28" y1="28" x2="10" y2="14" stroke="currentColor" strokeWidth="1.5" opacity="0.5" />
              <line x1="28" y1="28" x2="46" y2="14" stroke="currentColor" strokeWidth="1.5" opacity="0.5" />
              <line x1="28" y1="28" x2="10" y2="42" stroke="currentColor" strokeWidth="1.5" opacity="0.4" />
              <line x1="28" y1="28" x2="46" y2="42" stroke="currentColor" strokeWidth="1.5" opacity="0.4" />
            </svg>
            <Box sx={{ textAlign: "center" }}>
              <Typography variant="h6" fontWeight={700} sx={{ color: "var(--text-primary)", mb: 0.5 }}>
                Graph Explorer
              </Typography>
              <Typography variant="body2" sx={{ color: "var(--text-secondary)" }}>
                Search for a person to visualize their network and connections.
              </Typography>
            </Box>
          </Box>
          {searchAutocomplete}
        </Box>
      </Box>
    );
  }


  return (
    <Box sx={{ height: "100%", minHeight: 0, color: "var(--text-primary)", display: "flex", flexDirection: "column", gap: "18px" }}>
      {!pureGraph && <Box sx={{ px: 1.5, pt: "18px", pb: 0.5 }}>{breadcrumb}</Box>}
      <Box sx={{ flex: 1, minHeight: 0 }}>
      <PersonGraphViewer
        key={`${current.elementId ?? ""}-${current.personId ?? ""}`}
        personId={current.personId}
        elementId={current.elementId}
        title={current.title}
        overlayMode={overlayMode}
        nodeSizeScale={pureGraph ? 0.78 : 1}
        layoutScale={pureGraph ? 0.72 : 1}
        fitPadding={pureGraph ? 42 : 90}
        hideControls={pureGraph}
        extraOverlay={navOverlay}
        onNavigateNode={handleNavigateNode}
        onNodeContextMenu={handleNodeContextMenu}
      />
      </Box>
      <Menu
        open={contextMenu !== null}
        onClose={() => setContextMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={contextMenu !== null ? { top: contextMenu.mouseY, left: contextMenu.mouseX } : undefined}
        slotProps={{ paper: { sx: { bgcolor: "var(--bg-card)", color: "var(--text-primary)", border: "1px solid var(--border)" } } }}
      >
        <MenuItem onClick={handleOpenInNewTab}>Open graph in new tab</MenuItem>
        {contextMenu?.personId ? (
          <MenuItem onClick={handleOpenPersonPage}>Open person page in new tab</MenuItem>
        ) : null}
      </Menu>
    </Box>
  );
}
