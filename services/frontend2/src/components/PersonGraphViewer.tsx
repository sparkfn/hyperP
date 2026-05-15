"use client";

import {
  useCallback,
  useEffect,
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
import PersonIcon from "@mui/icons-material/Person";
import HomeIcon from "@mui/icons-material/Home";
import DescriptionIcon from "@mui/icons-material/Description";
import VpnKeyIcon from "@mui/icons-material/VpnKey";
import ChangeHistoryIcon from "@mui/icons-material/ChangeHistory";
import AssignmentIcon from "@mui/icons-material/Assignment";
import MergeTypeIcon from "@mui/icons-material/MergeType";
import ReceiptIcon from "@mui/icons-material/Receipt";
import InventoryIcon from "@mui/icons-material/Inventory";
import CircleIcon from "@mui/icons-material/Circle";
import StorageIcon from "@mui/icons-material/Storage";
import GavelIcon from "@mui/icons-material/Gavel";
import StorefrontIcon from "@mui/icons-material/Storefront";
import dynamic from "next/dynamic";
import type { ForceGraphMethods } from "react-force-graph-2d";

import { BffError, bffFetch } from "@/lib/api-client";
import type { PersonGraph } from "@/lib/api-types";
import {
  colorForLabel,
  iconForLabel,
  paintNode,
  paintNodePointerArea,
  toForceGraphData,
  NODE_SIZE,
  type FGGraphData,
  type FGLink,
  type FGNode,
  type NodeIcon,
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

const ICON_COMPONENTS: Record<NodeIcon, ReactElement> = {
  person: <PersonIcon />,
  home: <HomeIcon />,
  description: <DescriptionIcon />,
  vpnKey: <VpnKeyIcon />,
  diamond: <ChangeHistoryIcon />,
  assignment: <AssignmentIcon />,
  mergeType: <MergeTypeIcon />,
  receipt: <ReceiptIcon />,
  inventory: <InventoryIcon />,
  bullet: <CircleIcon sx={{ fontSize: 12 }} />,
  dataSource: <StorageIcon />,
  gavel: <GavelIcon />,
  storefront: <StorefrontIcon />,
};

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
        bgcolor: "var(--bg-card)",
        color: "var(--text-primary)",
        border: "1px solid var(--border)",
        borderRadius: "8px",
      }}
    >
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
        {labels.map((label) => (
          <Chip
            key={label}
            icon={ICON_COMPONENTS[iconForLabel(label)]}
            label={label}
            size="small"
            sx={{ bgcolor: colorForLabel(label), color: "#fff", "& .MuiChip-icon": { color: "#fff" } }}
          />
        ))}
      </Stack>
    </Paper>
  );
}

interface PersonGraphViewerProps {
  personId?: string;
  elementId?: string;
  onNavigateNode?: (elementId: string, label: string, displayName: string) => void;
  onNodeContextMenu?: (
    elementId: string,
    label: string,
    displayName: string,
    position: { x: number; y: number },
    properties: Record<string, string | number | boolean | null>,
  ) => void;
}

export default function PersonGraphViewer({
  personId,
  elementId,
  onNavigateNode,
  onNodeContextMenu,
}: PersonGraphViewerProps): ReactElement {
  const [graphData, setGraphData] = useState<FGGraphData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<SelectedItem | null>(null);
  const [maxHops, setMaxHops] = useState(2);
  const [dimensions, setDimensions] = useState({ width: 800, height: 500 });
  const [uniqueLabels, setUniqueLabels] = useState<string[]>([]);

  const graphRef = useRef<ForceGraphMethods<AnyNode, AnyLink> | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement>(null);
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Responsive container
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setDimensions({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // Fetch graph data
  useEffect(() => {
    const id = elementId ?? personId;
    if (!id) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);

    const params = new URLSearchParams();
    if (elementId) params.set("element_id", elementId);
    if (personId) params.set("person_id", personId);
    params.set("max_hops", String(maxHops));

    const suffix = elementId ? `/bff/persons/graph/node?${params.toString()}` : `/bff/persons/${encodeURIComponent(personId!)}/graph?${params.toString()}`;
    bffFetch<PersonGraph>(suffix)
      .then((data) => {
        const fgData = toForceGraphData(data, personId, elementId);
        setGraphData(fgData);
        setUniqueLabels([...new Set(fgData.nodes.map((n) => n.label))].sort());
      })
      .catch((err: unknown) => {
        if (err instanceof BffError) {
          setError(err.message);
        } else {
          setError("Failed to load graph data");
        }
      })
      .finally(() => setLoading(false));
  }, [personId, elementId, maxHops]);

  const handleNodeClick = useCallback(
    (raw: AnyNode) => {
      const node = raw as unknown as FGNode & { x?: number; y?: number };
      // Distinguish single-click (select) from double-click (navigate)
      if (clickTimerRef.current !== null) {
        clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
        // Double-click: navigate
        if (onNavigateNode) {
          onNavigateNode(node.id, node.label, node.displayName);
        }
        return;
      }
      clickTimerRef.current = setTimeout(() => {
        clickTimerRef.current = null;
        setSelected({ kind: "node", data: node });
      }, DOUBLE_CLICK_MS);
    },
    [onNavigateNode],
  );

  const handleNodeRightClick = useCallback(
    (raw: AnyNode, event: MouseEvent) => {
      event.preventDefault();
      if (!onNodeContextMenu) return;
      const node = raw as unknown as FGNode & { x?: number; y?: number };
      onNodeContextMenu(node.id, node.label, node.displayName, { x: event.clientX, y: event.clientY }, node.properties);
    },
    [onNodeContextMenu],
  );

  const handleLinkClick = useCallback((raw: AnyLink) => {
    const link = raw as unknown as FGLink & { source?: unknown; target?: unknown };
    setSelected({ kind: "edge", data: link });
  }, []);

  return (
    <Stack spacing={1} sx={{ height: "100%", color: "var(--text-primary)" }}>
      <Stack direction="row" spacing={1} alignItems="center">
        <TextField
          id="graph-max-hops"
          select
          size="small"
          label="Max hops"
          value={maxHops}
          onChange={(e) => setMaxHops(Number(e.target.value))}
          slotProps={{
            inputLabel: { htmlFor: undefined },
            select: {
              MenuProps: {
                slotProps: {
                  paper: {
                    sx: {
                      bgcolor: "var(--bg-card)",
                      color: "var(--text-primary)",
                      border: "1px solid var(--border)",
                    },
                  },
                },
              },
            },
          }}
          sx={{
            width: 120,
            "& .MuiOutlinedInput-root": {
              color: "var(--text-primary)",
              backgroundColor: "var(--bg-card)",
              "& fieldset": { borderColor: "var(--border)" },
              "&:hover fieldset": { borderColor: "var(--border-strong)" },
              "&.Mui-focused fieldset": { borderColor: "var(--accent)" },
            },
            "& .MuiInputLabel-root": { color: "var(--text-muted)" },
            "& .MuiSvgIcon-root": { color: "var(--text-muted)" },
          }}
        >
          {[1, 2, 3, 4].map((n) => (
            <MenuItem key={n} value={n}>
              {n} hop{n > 1 ? "s" : ""}
            </MenuItem>
          ))}
        </TextField>
        {graphData !== null ? (
          <Typography variant="body2" sx={{ color: "var(--text-secondary)" }}>
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
          borderColor: "var(--border)",
          borderRadius: "10px",
          bgcolor: "var(--bg-surface-2)",
          backgroundImage: "radial-gradient(circle, var(--border-strong) 1px, transparent 1px)",
          backgroundSize: "24px 24px",
        }}
      >
        {graphData !== null && !loading ? (
          <>
            <ForceGraph2D
              ref={graphRef}
              graphData={graphData}
              backgroundColor="rgba(0,0,0,0)"
              width={dimensions.width}
              height={dimensions.height}
              nodeId="id"
              linkSource="source"
              linkTarget="target"
              nodeVal={() => NODE_SIZE * 3}
              nodeCanvasObject={paintNode}
              nodeCanvasObjectMode={() => "replace"}
              nodePointerAreaPaint={paintNodePointerArea}
              linkLabel={(raw: AnyLink) => (raw as unknown as FGLink).type}
              linkColor={() => "#b0bec5"}
              linkWidth={1.5}
              linkDirectionalArrowLength={4}
              linkDirectionalArrowRelPos={1}
              onNodeClick={handleNodeClick}
              onNodeRightClick={onNodeContextMenu ? handleNodeRightClick : undefined}
              onLinkClick={handleLinkClick}
              enableNodeDrag
              cooldownTicks={300}
              d3AlphaDecay={0.01}
              d3VelocityDecay={0.3}
              warmupTicks={100}
            />
            <Legend labels={uniqueLabels} />
            {selected !== null ? (
              <GraphDetailPanel
                item={selected}
                onClose={() => setSelected(null)}
                onOpenGraph={onNavigateNode ?? (() => {})}
              />
            ) : null}
          </>
        ) : null}
      </Box>
    </Stack>
  );
}