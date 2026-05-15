"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { keyframes } from "@mui/system";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
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
import ShoppingCartIcon from "@mui/icons-material/ShoppingCart";
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
const LINK_DISTANCE = 72;
const NODE_COLLISION_RADIUS = 32;
const MANY_BODY_STRENGTH = -260;

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
  shoppingCart: <ShoppingCartIcon />,
  dataSource: <StorageIcon />,
  gavel: <GavelIcon />,
  storefront: <StorefrontIcon />,
};

const graphPulse = keyframes`
  0%, 100% { opacity: 0.35; }
  50%       { opacity: 0.75; }
`;

function GraphLoadingSkeleton(): ReactElement {
  return (
    <Box
      sx={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        pointerEvents: "none",
        color: "var(--border-strong)",
      }}
    >
      <Box
        component="svg"
        viewBox="0 0 100 100"
        preserveAspectRatio="xMidYMid meet"
        sx={{ width: "min(28%, 160px)", height: "min(28%, 160px)" }}
      >
        {/* Orbit tracks */}
        <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4" opacity="0.25" />
        <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3" opacity="0.18" />

        {/* Center node */}
        <circle cx="50" cy="50" r="7" fill="currentColor" opacity="0.55" />

        {/* Inner orbit — 2 nodes, 3s CW */}
        <g>
          <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
          <circle cx="72" cy="50" r="5" fill="currentColor" opacity="0.65" />
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
          <circle cx="72" cy="50" r="4" fill="currentColor" opacity="0.5" />
        </g>

        {/* Outer orbit — 3 nodes, 5s CCW */}
        <g>
          <animateTransform attributeName="transform" type="rotate" from="60 50 50" to="-300 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
          <circle cx="86" cy="50" r="4" fill="currentColor" opacity="0.55" />
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="-180 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
          <circle cx="86" cy="50" r="3.5" fill="currentColor" opacity="0.45" />
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="300 50 50" to="-60 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
          <circle cx="86" cy="50" r="3" fill="currentColor" opacity="0.4" />
        </g>
      </Box>
    </Box>
  );
}

function Legend({ labels }: { labels: string[] }): ReactElement {
  return (
    <Stack spacing={0.75}>
      <Typography
        variant="caption"
        sx={{ color: "var(--text-muted)", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}
      >
        Node types · {labels.length}
      </Typography>
      <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
        {labels.map((label) => (
          <Chip
            key={label}
            icon={ICON_COMPONENTS[iconForLabel(label)]}
            label={label}
            size="small"
            sx={{
              height: 24,
              bgcolor: colorForLabel(label),
              color: "#fff",
              fontWeight: 600,
              "& .MuiChip-icon": { color: "#fff", fontSize: 16 },
            }}
          />
        ))}
      </Stack>
    </Stack>
  );
}

const HOP_SELECT_SX = {
  "& .MuiOutlinedInput-root": {
    color: "var(--text-primary)",
    backgroundColor: "var(--bg-card)",
    "& fieldset": { borderColor: "var(--border)" },
    "&:hover fieldset": { borderColor: "var(--border-strong)" },
    "&.Mui-focused fieldset": { borderColor: "var(--accent)" },
  },
  "& .MuiInputLabel-root": { color: "var(--text-muted)" },
  "& .MuiSvgIcon-root": { color: "var(--text-muted)" },
} as const;

const HOP_SELECT_PROPS = {
  inputLabel: { htmlFor: undefined as undefined },
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
} as const;

interface PersonGraphViewerProps {
  title: string;
  personId?: string;
  elementId?: string;
  /** When true: full-canvas layout with overlay controls; no left panel. */
  overlayMode?: boolean;
  /** Extra overlay nodes rendered inside the canvas container (e.g. maximize button). */
  extraOverlay?: ReactNode;
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
  title,
  personId,
  elementId,
  overlayMode = false,
  extraOverlay,
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

  useEffect(() => {
    if (graphData === null) return;
    const graph = graphRef.current;
    if (!graph) return;

    const linkForce = graph.d3Force("link");
    if (linkForce && "distance" in linkForce && typeof linkForce.distance === "function") {
      linkForce.distance(LINK_DISTANCE);
    }

    const chargeForce = graph.d3Force("charge");
    if (chargeForce && "strength" in chargeForce && typeof chargeForce.strength === "function") {
      chargeForce.strength(MANY_BODY_STRENGTH);
    }

    graph.d3Force("collide", ((alpha: number) => {
      const nodes = graphData.nodes as Array<FGNode & { x?: number; y?: number; vx?: number; vy?: number }>;
      for (let i = 0; i < nodes.length; i += 1) {
        const a = nodes[i];
        if (!a) continue;
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = nodes[j];
          if (!b) continue;
          const dx = (b.x ?? 0) - (a.x ?? 0);
          const dy = (b.y ?? 0) - (a.y ?? 0);
          const distance = Math.hypot(dx, dy) || 1;
          const overlap = NODE_COLLISION_RADIUS - distance;
          if (overlap <= 0) continue;
          const move = (overlap / distance) * alpha * 0.5;
          const mx = dx * move;
          const my = dy * move;
          a.vx = (a.vx ?? 0) - mx;
          a.vy = (a.vy ?? 0) - my;
          b.vx = (b.vx ?? 0) + mx;
          b.vy = (b.vy ?? 0) + my;
        }
      }
    }) as never);
    graph.d3ReheatSimulation();
    window.setTimeout(() => graph.zoomToFit(500, 90), 150);
  }, [graphData, dimensions.width, dimensions.height]);

  const handleNodeClick = useCallback(
    (raw: AnyNode) => {
      const node = raw as unknown as FGNode & { x?: number; y?: number };
      if (clickTimerRef.current !== null) {
        clearTimeout(clickTimerRef.current);
        clickTimerRef.current = null;
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

  const forceGraphCanvas = graphData !== null && !loading ? (
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
      {selected !== null ? (
        <GraphDetailPanel
          item={selected}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </>
  ) : null;

  // ── Overlay mode: full-canvas, controls as overlays ──────────────────────────
  if (overlayMode) {
    return (
      <Box
        ref={containerRef}
        sx={{
          position: "relative",
          width: "100%",
          height: "100%",
          minHeight: 0,
          bgcolor: "var(--bg-surface-2)",
          backgroundImage: "radial-gradient(circle, var(--border-strong) 1px, transparent 1px)",
          backgroundSize: "24px 24px",
          overflow: "hidden",
        }}
      >
        {forceGraphCanvas}

        {loading ? <GraphLoadingSkeleton /> : null}
        {error !== null ? (
          <Typography
            variant="body2"
            color="error"
            sx={{ position: "absolute", top: 12, left: 12 }}
          >
            {error}
          </Typography>
        ) : null}

        {/* Top-left: hops selector */}
        <Box sx={{ position: "absolute", top: 12, left: 12, zIndex: 10 }}>
          <TextField
            id="graph-max-hops-overlay"
            select
            size="small"
            label="Max hops"
            value={maxHops}
            onChange={(e) => setMaxHops(Number(e.target.value))}
            slotProps={HOP_SELECT_PROPS}
            sx={{ width: 140, ...HOP_SELECT_SX }}
          >
            {[1, 2, 3, 4].map((n) => (
              <MenuItem key={n} value={n}>
                {n} hop{n > 1 ? "s" : ""}
              </MenuItem>
            ))}
          </TextField>
        </Box>

        {/* Bottom-left: node type legend */}
        {uniqueLabels.length > 0 ? (
          <Box
            sx={{
              position: "absolute",
              bottom: 12,
              left: 12,
              zIndex: 10,
              maxWidth: 360,
              bgcolor: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              p: 1,
            }}
          >
            <Legend labels={uniqueLabels} />
          </Box>
        ) : null}

        {/* Extra overlays (e.g. maximize button) */}
        {extraOverlay}
      </Box>
    );
  }

  // ── Default mode: two-column layout (side panel + canvas) ────────────────────
  const stats = graphData
    ? [
        { label: "Nodes", value: graphData.nodes.length },
        { label: "Edges", value: graphData.links.length },
        { label: "Types", value: uniqueLabels.length },
      ]
    : [];

  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: { xs: "1fr", md: "300px minmax(0, 1fr)" },
        gap: 1.5,
        height: "100%",
        minHeight: 0,
        color: "var(--text-primary)",
      }}
    >
      <Paper
        elevation={0}
        sx={{
          display: "flex",
          flexDirection: "column",
          gap: 1.5,
          minHeight: 0,
          p: 1.5,
          bgcolor: "var(--bg-surface-2)",
          border: "1px solid var(--border)",
          borderRadius: "10px",
        }}
      >
        <Stack spacing={0.5}>
          <Typography
            variant="caption"
            sx={{ color: "var(--text-muted)", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}
          >
            Focused graph
          </Typography>
          <Typography variant="subtitle1" fontWeight={700} sx={{ lineHeight: 1.25, color: "var(--text-primary)" }}>
            {title}
          </Typography>
          <Typography variant="body2" sx={{ color: "var(--text-secondary)" }}>
            Explore related people, identifiers, addresses, review cases, and source records around this person.
          </Typography>
        </Stack>

        <Divider sx={{ borderColor: "var(--border)" }} />

        <Stack spacing={1}>
          <Typography
            variant="caption"
            sx={{ color: "var(--text-muted)", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}
          >
            Scope
          </Typography>
          <TextField
            id="graph-max-hops"
            select
            size="small"
            label="Max hops"
            value={maxHops}
            onChange={(e) => setMaxHops(Number(e.target.value))}
            slotProps={HOP_SELECT_PROPS}
            sx={HOP_SELECT_SX}
          >
            {[1, 2, 3, 4].map((n) => (
              <MenuItem key={n} value={n}>
                {n} hop{n > 1 ? "s" : ""}
              </MenuItem>
            ))}
          </TextField>
        </Stack>

        <Stack spacing={1}>
          <Typography
            variant="caption"
            sx={{ color: "var(--text-muted)", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}
          >
            Summary
          </Typography>
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 0.75 }}>
            {loading ? (
              ["Nodes", "Edges", "Types"].map((label) => (
                <Paper
                  key={label}
                  elevation={0}
                  sx={{ p: 1, bgcolor: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "8px" }}
                >
                  <Box
                    sx={{
                      width: 32, height: 20, borderRadius: "4px", mb: 0.5,
                      bgcolor: "var(--border)",
                      animation: `${graphPulse} 1.6s ease-in-out infinite`,
                    }}
                  />
                  <Typography variant="caption" sx={{ color: "var(--text-muted)" }}>{label}</Typography>
                </Paper>
              ))
            ) : (
              stats.map((stat) => (
                <Paper
                  key={stat.label}
                  elevation={0}
                  sx={{ p: 1, bgcolor: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "8px" }}
                >
                  <Typography variant="h6" sx={{ color: "var(--text-primary)", fontSize: 20, fontWeight: 700, lineHeight: 1 }}>
                    {stat.value}
                  </Typography>
                  <Typography variant="caption" sx={{ color: "var(--text-muted)" }}>
                    {stat.label}
                  </Typography>
                </Paper>
              ))
            )}
          </Box>
        </Stack>

        <Divider sx={{ borderColor: "var(--border)" }} />

        <Legend labels={uniqueLabels} />

        <Box sx={{ flexGrow: 1 }} />

        <Stack spacing={0.75}>
          <Typography
            variant="caption"
            sx={{ color: "var(--text-muted)", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}
          >
            Interactions
          </Typography>
          <Typography variant="body2" sx={{ color: "var(--text-secondary)", fontSize: 12, lineHeight: 1.6 }}>
            Hint: click a node to inspect details, double-click to open its focused graph, or right-click for node actions.
          </Typography>
        </Stack>
      </Paper>

      <Box sx={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
        {error !== null ? <Typography color="error" sx={{ mb: 1 }}>{error}</Typography> : null}

        <Box
          ref={containerRef}
          sx={{
            position: "relative",
            flexGrow: 1,
            minHeight: 0,
            border: 1,
            borderColor: "var(--border)",
            borderRadius: "10px",
            bgcolor: "var(--bg-surface-2)",
            backgroundImage: "radial-gradient(circle, var(--border-strong) 1px, transparent 1px)",
            backgroundSize: "24px 24px",
            boxShadow: "inset 0 1px 0 color-mix(in srgb, var(--bg-card) 82%, transparent)",
            overflow: "hidden",
          }}
        >
          {loading ? <GraphLoadingSkeleton /> : forceGraphCanvas}
        </Box>
      </Box>
    </Box>
  );
}
