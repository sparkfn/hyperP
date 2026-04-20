"use client";

import { useCallback, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

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
  height: number | string;
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
  height,
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

  return (
    <Stack spacing={1} sx={{ height: "100%" }}>
      <Typography variant="body2" color="text.secondary">
        Double-click any node to open graph in a new tab. Right-click for more options.
      </Typography>
      <Typography variant="subtitle1" fontWeight={600}>
        {initialTitle}
      </Typography>
      <Box sx={{ height, flexGrow: 1, minHeight: 0 }}>
        <PersonGraphViewer
          personId={initialPersonId}
          elementId={initialElementId}
          onNavigateNode={handleNavigateNode}
          onNodeContextMenu={handleNodeContextMenu}
        />
      </Box>
      <Menu
        open={contextMenu !== null}
        onClose={() => setContextMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={
          contextMenu !== null ? { top: contextMenu.mouseY, left: contextMenu.mouseX } : undefined
        }
      >
        <MenuItem onClick={handleOpenInNewTab}>Open graph in new tab</MenuItem>
        {contextMenu?.personId ? (
          <MenuItem onClick={handleOpenPersonPage}>Open person page in new tab</MenuItem>
        ) : null}
      </Menu>
    </Stack>
  );
}
