"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
} from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import CloseIcon from "@mui/icons-material/Close";
import dynamic from "next/dynamic";

import { BffError, bffFetch } from "@/lib/api-client";
import type { GraphNode, PersonGraph } from "@/lib/api-types";

// react-force-graph-2d uses canvas and must not SSR.
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), {
  ssr: false,
});

// --- Node color palette keyed by graph label ---

const LABEL_COLORS: Record<string, string> = {
  Person: "#1f4e9e",
  Identifier: "#7e57c2",
  Address: "#00897b",
  SourceRecord: "#ef6c00",
  SourceSystem: "#6d4c41",
  MatchDecision: "#c62828",
  ReviewCase: "#ad1457",
  MergeEvent: "#37474f",
};

const DEFAULT_NODE_COLOR = "#78909c";

function colorForLabel(label: string): string {
  return LABEL_COLORS[label] ?? DEFAULT_NODE_COLOR;
}

// --- Types for the force-graph data model ---

interface FGNode {
  id: string;
  label: string;
  displayName: string;
  color: string;
  isFocus: boolean;
  properties: Record<string, string | number | boolean | null>;
}

interface FGLink {
  id: string;
  source: string;
  target: string;
  type: string;
  properties: Record<string, string | number | boolean | null>;
}

interface FGGraphData {
  nodes: FGNode[];
  links: FGLink[];
}

function displayNameForNode(node: GraphNode): string {
  const p = node.properties;
  if (node.label === "Person") {
    return (p["preferred_full_name"] as string | null) ?? (p["person_id"] as string | null) ?? node.id;
  }
  if (node.label === "Identifier") {
    const idType = p["identifier_type"] as string | null;
    const val = p["normalized_value"] as string | null;
    return idType && val ? `${idType}: ${val}` : node.id;
  }
  if (node.label === "Address") {
    return (p["normalized_full"] as string | null) ?? node.id;
  }
  if (node.label === "SourceRecord") {
    return (p["source_record_id"] as string | null) ?? node.id;
  }
  if (node.label === "SourceSystem") {
    return (p["source_key"] as string | null) ?? node.id;
  }
  return node.id;
}

function toForceGraphData(graph: PersonGraph, focusPersonId?: string, focusElementId?: string): FGGraphData {
  const visibleNodes = graph.nodes.filter((n) => !HIDDEN_LABELS.has(n.label));
  const nodeIds = new Set(visibleNodes.map((n) => n.id));
  return {
    nodes: visibleNodes.map((n) => {
      const isFocus = focusElementId
        ? n.id === focusElementId
        : (n.properties["person_id"] as string | undefined) === focusPersonId;
      return {
        id: n.id,
        label: n.label,
        displayName: displayNameForNode(n),
        color: colorForLabel(n.label),
        isFocus: isFocus ?? false,
        properties: n.properties,
      };
    }),
    links: graph.edges
      .filter(
        (e) =>
          !HIDDEN_REL_TYPES.has(e.type) &&
          nodeIds.has(e.source) &&
          nodeIds.has(e.target),
      )
      .map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        type: e.type,
        properties: e.properties,
      })),
  };
}

// --- Detail panel for selected node or edge ---

type SelectedItem =
  | { kind: "node"; data: FGNode }
  | { kind: "edge"; data: FGLink };

interface DetailPanelProps {
  item: SelectedItem;
  onClose: () => void;
  onOpenGraph: (elementId: string, label: string, displayName: string) => void;
}

function DetailPanel({ item, onClose, onOpenGraph }: DetailPanelProps): ReactElement {
  const title = item.kind === "node" ? item.data.label : item.data.type;
  const subtitle =
    item.kind === "node" ? item.data.displayName : `${item.data.type} relationship`;
  const props = item.kind === "node" ? item.data.properties : item.data.properties;

  const isNode = item.kind === "node";

  return (
    <Paper
      elevation={4}
      sx={{
        position: "absolute",
        top: 16,
        right: 16,
        width: 340,
        maxHeight: "calc(100% - 32px)",
        overflow: "auto",
        zIndex: 10,
        p: 2,
      }}
    >
      <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
        <Chip
          label={title}
          size="small"
          sx={{ bgcolor: item.kind === "node" ? item.data.color : "#546e7a", color: "#fff" }}
        />
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>
      <Typography variant="subtitle2" sx={{ mt: 1 }}>
        {subtitle}
      </Typography>
      {isNode ? (
        <Typography
          variant="caption"
          color="primary"
          sx={{ cursor: "pointer", textDecoration: "underline", display: "block", mb: 1 }}
          onClick={() => onOpenGraph(item.data.id, item.data.label, item.data.displayName)}
        >
          Open graph from this node
        </Typography>
      ) : null}
      <Divider sx={{ my: 1 }} />
      <Stack spacing={0.5}>
        {Object.entries(props).map(([key, val]) => (
          <Box key={key}>
            <Typography variant="caption" color="text.secondary">
              {key}
            </Typography>
            <Typography variant="body2" sx={{ wordBreak: "break-all" }}>
              {val === null ? "null" : String(val)}
            </Typography>
          </Box>
        ))}
        {Object.keys(props).length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No properties
          </Typography>
        ) : null}
      </Stack>
    </Paper>
  );
}

// --- Legend ---

function Legend({ labels }: { labels: string[] }): ReactElement {
  return (
    <Paper
      elevation={2}
      sx={{
        position: "absolute",
        bottom: 16,
        left: 16,
        zIndex: 10,
        px: 2,
        py: 1,
      }}
    >
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
        {labels.map((label) => (
          <Chip
            key={label}
            label={label}
            size="small"
            sx={{ bgcolor: colorForLabel(label), color: "#fff", fontSize: "0.7rem" }}
          />
        ))}
      </Stack>
    </Paper>
  );
}

// --- Main component ---

// Labels and relationship types hidden from the interactive graph.
const HIDDEN_LABELS = new Set(["SourceSystem"]);
const HIDDEN_REL_TYPES = new Set(["FROM_SOURCE"]);

// The force-graph library types callbacks with its own NodeObject / LinkObject
// which use `[others: string]: any`. We use Record<string, unknown> as the
// intermediary and cast the fields we placed on each datum.
type AnyNode = Record<string, unknown>;
type AnyLink = Record<string, unknown>;

const DOUBLE_CLICK_MS = 300;

interface PersonGraphViewerProps {
  /** When set, fetch graph by person_id (person-centric endpoint). */
  personId?: string;
  /** When set, fetch graph by Neo4j elementId (generic node endpoint). */
  elementId?: string;
  /** Called when a node is double-clicked — receives the elementId and label. */
  onNavigateNode: (elementId: string, label: string, displayName: string) => void;
}

export default function PersonGraphViewer({
  personId,
  elementId,
  onNavigateNode,
}: PersonGraphViewerProps): ReactElement {
  const [maxHops, setMaxHops] = useState<number>(2);
  const [graphData, setGraphData] = useState<FGGraphData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectedItem | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState<{ width: number; height: number }>({
    width: 800,
    height: 600,
  });
  const lastClickRef = useRef<{ nodeId: string; time: number }>({ nodeId: "", time: 0 });

  // Measure container size
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) {
        setDimensions({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Fetch graph data when the target or maxHops changes.
  const fetchKey = personId ?? elementId ?? "";

  useEffect(() => {
    if (!fetchKey) return;
    let cancelled = false;
    async function fetchGraph(): Promise<void> {
      setLoading(true);
      setError(null);
      setSelected(null);
      try {
        const url = personId
          ? `/api/persons/${encodeURIComponent(personId)}/graph?max_hops=${maxHops}`
          : `/api/persons/graph/node?element_id=${encodeURIComponent(elementId ?? "")}&max_hops=${maxHops}`;
        const graph = await bffFetch<PersonGraph>(url);
        if (!cancelled) {
          setGraphData(toForceGraphData(graph, personId, elementId));
        }
      } catch (err: unknown) {
        if (!cancelled) {
          const msg =
            err instanceof BffError || err instanceof Error ? err.message : "Failed to load graph.";
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void fetchGraph();
    return () => {
      cancelled = true;
    };
  }, [fetchKey, maxHops, personId, elementId]);

  const handleNodeClick = useCallback(
    (raw: AnyNode) => {
      const node = raw as unknown as FGNode;
      const now = Date.now();
      const prev = lastClickRef.current;

      if (prev.nodeId === node.id && now - prev.time < DOUBLE_CLICK_MS) {
        // Double-click detected — open a new graph centered on this node.
        lastClickRef.current = { nodeId: "", time: 0 };
        onNavigateNode(node.id, node.label, node.displayName);
        return;
      }

      lastClickRef.current = { nodeId: node.id, time: now };
      setSelected({ kind: "node", data: node });
    },
    [onNavigateNode],
  );

  const handleLinkClick = useCallback(
    (raw: AnyLink) => {
      const link = raw as unknown as FGLink;
      setSelected({ kind: "edge", data: link });
    },
    [],
  );

  const uniqueLabels = useMemo(() => {
    if (!graphData) return [];
    return [...new Set(graphData.nodes.map((n) => n.label))].sort();
  }, [graphData]);

  const nodeCanvasObject = useCallback(
    (raw: AnyNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const node = raw as unknown as FGNode & { x?: number; y?: number };
      const fontSize = 12 / globalScale;
      const nodeSize = 6;
      const x = node.x ?? 0;
      const y = node.y ?? 0;

      // Focus indicator — pulsing outer ring
      if (node.isFocus) {
        ctx.beginPath();
        ctx.arc(x, y, nodeSize + 4, 0, 2 * Math.PI);
        ctx.strokeStyle = "#ff9800";
        ctx.lineWidth = 2.5 / globalScale;
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(x, y, nodeSize + 7, 0, 2 * Math.PI);
        ctx.strokeStyle = "rgba(255, 152, 0, 0.3)";
        ctx.lineWidth = 3 / globalScale;
        ctx.stroke();
      }

      // Draw circle
      ctx.beginPath();
      ctx.arc(x, y, nodeSize, 0, 2 * Math.PI);
      ctx.fillStyle = node.color;
      ctx.fill();

      // Draw label
      ctx.font = `${fontSize}px Inter, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = "#333";
      const text =
        node.displayName.length > 24
          ? node.displayName.slice(0, 22) + "..."
          : node.displayName;
      ctx.fillText(text, x, y + nodeSize + 2);
    },
    [],
  );

  const nodePointerAreaPaint = useCallback(
    (raw: AnyNode, color: string, ctx: CanvasRenderingContext2D) => {
      const node = raw as unknown as FGNode & { x?: number; y?: number };
      const nodeSize = 6;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      ctx.beginPath();
      ctx.arc(x, y, nodeSize + 4, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    [],
  );

  return (
    <Stack spacing={2} sx={{ height: "100%" }}>
      <Stack direction="row" alignItems="center" spacing={2}>
        <TextField
          id={`graph-max-hops-${fetchKey}`}
          select
          size="small"
          label="Max hops"
          value={maxHops}
          onChange={(e) => setMaxHops(Number(e.target.value))}
          slotProps={{ inputLabel: { htmlFor: undefined } }}
          sx={{ width: 120 }}
        >
          {[1, 2, 3, 4].map((n) => (
            <MenuItem key={n} value={n}>
              {n} hop{n > 1 ? "s" : ""}
            </MenuItem>
          ))}
        </TextField>
        {graphData !== null ? (
          <Typography variant="body2" color="text.secondary">
            {graphData.nodes.length} nodes, {graphData.links.length} edges
          </Typography>
        ) : null}
        {loading ? <CircularProgress size={20} /> : null}
      </Stack>

      {error !== null ? (
        <Typography color="error">{error}</Typography>
      ) : null}

      <Box
        ref={containerRef}
        sx={{
          position: "relative",
          flexGrow: 1,
          minHeight: 500,
          border: 1,
          borderColor: "divider",
          borderRadius: 1,
          overflow: "hidden",
          bgcolor: "#ffffff",
          backgroundImage: "radial-gradient(circle, #d0d0d0 1px, transparent 1px)",
          backgroundSize: "24px 24px",
        }}
      >
        {graphData !== null && !loading ? (
          <>
            <ForceGraph2D
              graphData={graphData}
              backgroundColor="rgba(0,0,0,0)"
              width={dimensions.width}
              height={dimensions.height}
              nodeId="id"
              linkSource="source"
              linkTarget="target"
              nodeCanvasObject={nodeCanvasObject}
              nodeCanvasObjectMode={() => "replace"}
              nodePointerAreaPaint={nodePointerAreaPaint}
              linkLabel={(raw: AnyLink) => (raw as unknown as FGLink).type}
              linkColor={() => "#b0bec5"}
              linkWidth={1.5}
              linkDirectionalArrowLength={4}
              linkDirectionalArrowRelPos={1}
              onNodeClick={handleNodeClick}
              onLinkClick={handleLinkClick}
              enableNodeDrag
              cooldownTicks={100}
            />
            <Legend labels={uniqueLabels} />
            {selected !== null ? (
              <DetailPanel
                item={selected}
                onClose={() => setSelected(null)}
                onOpenGraph={onNavigateNode}
              />
            ) : null}
          </>
        ) : null}
      </Box>
    </Stack>
  );
}
