"use client";

import { useCallback, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Tooltip from "@mui/material/Tooltip";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import OpenInFullIcon from "@mui/icons-material/OpenInFull";

import PersonGraphViewer from "@/components/PersonGraphViewer";

interface NavEntry {
  elementId?: string;
  personId?: string;
  title: string;
}

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
  onMaximize,
}: PersonFocusedGraphProps): ReactElement {
  const [navStack, setNavStack] = useState<NavEntry[]>([
    { elementId: initialElementId, personId: initialPersonId, title: initialTitle },
  ]);
  const [contextMenu, setContextMenu] = useState<NodeContextMenu | null>(null);

  const current = navStack[navStack.length - 1] ?? { personId: initialPersonId, elementId: initialElementId, title: initialTitle };
  const canGoBack = navStack.length > 1;

  const handleNavigateNode = useCallback(
    (elementId: string, _label: string, displayName: string): void => {
      setNavStack((prev) => [...prev, { elementId, title: displayName }]);
    },
    [],
  );

  function handleBack(): void {
    setNavStack((prev) => (prev.length > 1 ? prev.slice(0, -1) : prev));
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

  const overlayControls = (
    <Box sx={{ position: "absolute", top: 12, right: 12, zIndex: 10, display: "flex", gap: 1 }}>
      {canGoBack && (
        <Tooltip title="Back">
          <IconButton
            size="small"
            onClick={handleBack}
            sx={{
              bgcolor: "var(--bg-card)",
              border: "1px solid var(--border)",
              color: "var(--text-secondary)",
              "&:hover": { bgcolor: "var(--bg-surface-2)", color: "var(--text-primary)" },
            }}
          >
            <ArrowBackIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
      {overlayMode && onMaximize && (
        <Tooltip title="Maximize graph">
          <IconButton
            size="small"
            onClick={onMaximize}
            sx={{
              bgcolor: "var(--bg-card)",
              border: "1px solid var(--border)",
              color: "var(--text-secondary)",
              "&:hover": { bgcolor: "var(--bg-surface-2)", color: "var(--text-primary)" },
            }}
          >
            <OpenInFullIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
    </Box>
  );

  return (
    <Box sx={{ height: "100%", minHeight: 0, color: "var(--text-primary)" }}>
      <PersonGraphViewer
        key={`${current.elementId ?? ""}-${current.personId ?? ""}`}
        personId={current.personId}
        elementId={current.elementId}
        title={current.title}
        overlayMode={overlayMode}
        extraOverlay={overlayControls}
        onNavigateNode={handleNavigateNode}
        onNodeContextMenu={handleNodeContextMenu}
      />
      <Menu
        open={contextMenu !== null}
        onClose={() => setContextMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={
          contextMenu !== null ? { top: contextMenu.mouseY, left: contextMenu.mouseX } : undefined
        }
        slotProps={{
          paper: {
            sx: {
              bgcolor: "var(--bg-card)",
              color: "var(--text-primary)",
              border: "1px solid var(--border)",
            },
          },
        }}
      >
        <MenuItem onClick={handleOpenInNewTab}>Open graph in new tab</MenuItem>
        {contextMenu?.personId ? (
          <MenuItem onClick={handleOpenPersonPage}>Open person page in new tab</MenuItem>
        ) : null}
      </Menu>
    </Box>
  );
}
