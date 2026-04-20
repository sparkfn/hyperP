"use client";

import { useCallback, useRef, useState, type ReactElement } from "react";

import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";

import PersonGraphViewer from "@/components/PersonGraphViewer";

interface GraphEntry {
  key: number;
  title: string;
  personId?: string;
  elementId?: string;
}

interface NodeContextMenu {
  mouseX: number;
  mouseY: number;
  elementId: string;
  label: string;
  displayName: string;
  personId: string | null;
}

interface GraphExplorerProps {
  initialPersonId?: string;
  initialElementId?: string;
  initialName?: string;
  initialLabel?: string;
}

function buildInitialGraphs(props: GraphExplorerProps): GraphEntry[] {
  if (props.initialPersonId) {
    return [{ key: 0, title: props.initialName ?? props.initialPersonId, personId: props.initialPersonId }];
  }
  if (props.initialElementId) {
    const title =
      props.initialLabel && props.initialName
        ? `${props.initialLabel}: ${props.initialName}`
        : props.initialName ?? props.initialElementId;
    return [{ key: 0, title, elementId: props.initialElementId }];
  }
  return [];
}

export default function GraphExplorer(props: GraphExplorerProps): ReactElement {
  const [graphs, setGraphs] = useState<GraphEntry[]>(() => buildInitialGraphs(props));
  const nextKeyRef = useRef<number>(graphs.length);
  const scrollTargetRef = useRef<number | null>(null);
  const [contextMenu, setContextMenu] = useState<NodeContextMenu | null>(null);

  const handleNavigateNode = useCallback(
    (elementId: string, label: string, displayName: string) => {
      const k = nextKeyRef.current++;
      scrollTargetRef.current = k;
      setGraphs((prev) => [{ key: k, title: `${label}: ${displayName}`, elementId }, ...prev]);
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
    ) => {
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

  function closeGraph(key: number): void {
    setGraphs((prev) => prev.filter((g) => g.key !== key));
  }

  function handleOpenInNewTab(): void {
    if (!contextMenu) return;
    const params = new URLSearchParams({
      element_id: contextMenu.elementId,
      label: contextMenu.label,
      name: contextMenu.displayName,
    });
    window.open(`/graph?${params.toString()}`, "_blank");
    setContextMenu(null);
  }

  function handleExpandHere(): void {
    if (!contextMenu) return;
    handleNavigateNode(contextMenu.elementId, contextMenu.label, contextMenu.displayName);
    setContextMenu(null);
  }

  function handleOpenPersonPage(): void {
    if (!contextMenu?.personId) return;
    window.open(`/persons/${encodeURIComponent(contextMenu.personId)}`, "_blank");
    setContextMenu(null);
  }

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Graph Explorer
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Double-click any node to expand its graph. Right-click to open in a new tab.
        </Typography>
      </Box>

      {graphs.length === 0 ? (
        <Paper elevation={0} variant="outlined" sx={{ p: 4, textAlign: "center" }}>
          <Typography color="text.secondary">
            No graphs open. Use the Person Search page to open a graph.
          </Typography>
        </Paper>
      ) : null}

      {graphs.map((entry) => (
        <Paper
          key={entry.key}
          ref={(el) => {
            if (el && scrollTargetRef.current === entry.key) {
              scrollTargetRef.current = null;
              el.scrollIntoView({ behavior: "smooth", block: "start" });
            }
          }}
          elevation={1}
          variant="outlined"
          sx={{ p: 2 }}
        >
          <Stack spacing={1}>
            <Stack direction="row" alignItems="center" justifyContent="space-between">
              <Typography variant="subtitle1" fontWeight={600}>
                {entry.title}
              </Typography>
              <IconButton size="small" onClick={() => closeGraph(entry.key)}>
                <CloseIcon fontSize="small" />
              </IconButton>
            </Stack>
            <Box sx={{ height: 600 }}>
              <PersonGraphViewer
                personId={entry.personId}
                elementId={entry.elementId}
                onNavigateNode={handleNavigateNode}
                onNodeContextMenu={handleNodeContextMenu}
              />
            </Box>
          </Stack>
        </Paper>
      ))}

      <Menu
        open={contextMenu !== null}
        onClose={() => setContextMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={
          contextMenu !== null ? { top: contextMenu.mouseY, left: contextMenu.mouseX } : undefined
        }
      >
        <MenuItem onClick={handleExpandHere}>Expand graph here</MenuItem>
        <MenuItem onClick={handleOpenInNewTab}>Open graph in new tab</MenuItem>
        {contextMenu?.personId ? (
          <MenuItem onClick={handleOpenPersonPage}>Open person page in new tab</MenuItem>
        ) : null}
      </Menu>
    </Stack>
  );
}
