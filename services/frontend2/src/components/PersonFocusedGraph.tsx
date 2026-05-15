"use client";

import { useCallback, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Tooltip from "@mui/material/Tooltip";
import OpenInFullIcon from "@mui/icons-material/OpenInFull";

import PersonGraphViewer from "@/components/PersonGraphViewer";

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
  const params = new URLSearchParams({
    element_id: elementId,
    label,
    name: displayName,
  });
  window.open(`/graph?${params.toString()}`, "_blank");
}

export default function PersonFocusedGraph({
  initialPersonId,
  initialElementId,
  initialTitle,
  overlayMode = false,
  onMaximize,
}: PersonFocusedGraphProps): ReactElement {
  const [contextMenu, setContextMenu] = useState<NodeContextMenu | null>(null);

  const handleNavigateNode = useCallback(
    (elementId: string, label: string, displayName: string): void => {
      openGraphTab(elementId, label, displayName);
    },
    [],
  );

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
      setContextMenu({
        mouseX: position.x,
        mouseY: position.y,
        elementId,
        label,
        displayName,
        personId,
      });
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

  const maximizeOverlay = overlayMode && onMaximize ? (
    <Box sx={{ position: "absolute", top: 12, right: 12, zIndex: 10 }}>
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
    </Box>
  ) : undefined;

  return (
    <Box sx={{ height: "100%", minHeight: 0, color: "var(--text-primary)" }}>
      <PersonGraphViewer
        personId={initialPersonId}
        elementId={initialElementId}
        title={initialTitle}
        overlayMode={overlayMode}
        extraOverlay={maximizeOverlay}
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
