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
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import dynamic from "next/dynamic";

import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonGraph } from "@/lib/api-types";
import {
  colorForLabel,
  toForceGraphData,
  type FGGraphData,
  type FGLink,
  type FGNode,
  type SelectedItem,
} from "@/components/graph-utils";
import GraphDetailPanel from "@/components/GraphDetailPanel";

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), {
  ssr: false,
});

// The force-graph library types callbacks with its own NodeObject / LinkObject
// which use `[others: string]: any`. We cast through Record<string, unknown>.
type AnyNode = Record<string, unknown>;
type AnyLink = Record<string, unknown>;

const DOUBLE_CLICK_MS = 300;

function Legend({ labels }: { labels: string[] }): ReactElement {
  return (
    <Paper
      elevation={2}
      sx={{ position: "absolute", bottom: 16, left: 16, zIndex: 10, px: 2, py: 1 }}
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

interface PersonGraphViewerProps {
  personId?: string;
  elementId?: string;
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
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });
  const lastClickRef = useRef<{ nodeId: string; time: number }>({ nodeId: "", time: 0 });

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setDimensions({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

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
        if (!cancelled) setGraphData(toForceGraphData(graph, personId, elementId));
      } catch (err: unknown) {
        if (!cancelled) {
          const msg = err instanceof BffError || err instanceof Error ? err.message : "Failed to load graph.";
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void fetchGraph();
    return () => { cancelled = true; };
  }, [fetchKey, maxHops, personId, elementId]);

  const handleNodeClick = useCallback(
    (raw: AnyNode) => {
      const node = raw as unknown as FGNode;
      const now = Date.now();
      const prev = lastClickRef.current;
      if (prev.nodeId === node.id && now - prev.time < DOUBLE_CLICK_MS) {
        lastClickRef.current = { nodeId: "", time: 0 };
        onNavigateNode(node.id, node.label, node.displayName);
        return;
      }
      lastClickRef.current = { nodeId: node.id, time: now };
      setSelected({ kind: "node", data: node });
    },
    [onNavigateNode],
  );

  const handleLinkClick = useCallback((raw: AnyLink) => {
    setSelected({ kind: "edge", data: raw as unknown as FGLink });
  }, []);

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

      ctx.beginPath();
      ctx.arc(x, y, nodeSize, 0, 2 * Math.PI);
      ctx.fillStyle = node.color;
      ctx.fill();

      ctx.font = `${fontSize}px Inter, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = "#333";
      const text = node.displayName.length > 24 ? node.displayName.slice(0, 22) + "..." : node.displayName;
      ctx.fillText(text, x, y + nodeSize + 2);
    },
    [],
  );

  const nodePointerAreaPaint = useCallback(
    (raw: AnyNode, color: string, ctx: CanvasRenderingContext2D) => {
      const node = raw as unknown as FGNode & { x?: number; y?: number };
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, 10, 0, 2 * Math.PI);
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

      {error !== null ? <Typography color="error">{error}</Typography> : null}

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
              <GraphDetailPanel
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
