/**
 * Shared types, colors, and data-conversion logic for the interactive
 * graph viewer. Extracted from PersonGraphViewer to keep module sizes
 * within project limits.
 *
 * Node icons use Path2D objects constructed from MUI SVG path data.
 * Each icon path is in a 24×24 viewBox, scaled to fit the node circle.
 * Path2D(svgPath) is supported in all modern browsers and renders
 * synchronously on the canvas — no async bitmap caching needed.
 */

import type { GraphNode, PersonGraph } from "@/lib/api-types";

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
  Order: "#2e7d32",
  Product: "#0277bd",
  LineItem: "#558b2f",
  Entity: "#6d4c41",
};

const DEFAULT_NODE_COLOR = "#78909c";

export function colorForLabel(label: string): string {
  return LABEL_COLORS[label] ?? DEFAULT_NODE_COLOR;
}

// --- Node icon type per graph label ---

export type NodeIcon =
  | "person"
  | "home"
  | "description"
  | "vpnKey"
  | "diamond"
  | "assignment"
  | "mergeType"
  | "receipt"
  | "inventory"
  | "bullet"
  | "dataSource"
  | "storefront";

const LABEL_ICONS: Record<string, NodeIcon> = {
  Person: "person",
  Identifier: "vpnKey",
  Address: "home",
  SourceRecord: "description",
  MatchDecision: "diamond",
  ReviewCase: "assignment",
  MergeEvent: "mergeType",
  Order: "receipt",
  Product: "inventory",
  LineItem: "bullet",
  SourceSystem: "dataSource",
  Entity: "storefront",
};

const DEFAULT_NODE_ICON: NodeIcon = "bullet";

export function iconForLabel(label: string): NodeIcon {
  return LABEL_ICONS[label] ?? DEFAULT_NODE_ICON;
}

// --- MUI SVG path data (24x24 viewBox) ---

const ICON_PATHS: Record<NodeIcon, string> = {
  person: "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4m0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4",
  home: "M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z",
  description: "M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8zm2 16H8v-2h8zm0-4H8v-2h8zm-3-5V3.5L18.5 9z",
  vpnKey: "M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2",
  diamond: "M12 7.77 18.39 18H5.61zM12 4 2 20h20z",
  assignment: "M19 3h-4.18C14.4 1.84 13.3 1 12 1s-2.4.84-2.82 2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2m-7 0c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1m2 14H7v-2h7zm3-4H7v-2h10zm0-4H7V7h10z",
  mergeType: "M17 20.41 18.41 19 15 15.59 13.59 17zM7.5 8H11v5.59L5.59 19 7 20.41l6-6V8h3.5L12 3.5z",
  receipt: "M18 17H6v-2h12zm0-4H6v-2h12zm0-4H6V7h12zM3 22l1.5-1.5L6 22l1.5-1.5L9 22l1.5-1.5L12 22l1.5-1.5L15 22l1.5-1.5L18 22l1.5-1.5L21 22V2l-1.5 1.5L18 2l-1.5 1.5L15 2l-1.5 1.5L12 2l-1.5 1.5L9 2 7.5 3.5 6 2 4.5 3.5 3 2z",
  inventory: "M20 2H4c-1 0-2 .9-2 2v3.01c0 .72.43 1.34 1 1.69V20c0 1.1 1.1 2 2 2h14c.9 0 2-.9 2-2V8.7c.57-.35 1-.97 1-1.69V4c0-1.1-1-2-2-2m-5 12H9v-2h6zm5-7H4V4l16-.02z",
  bullet: "",
  dataSource: "M2 20h20v-4H2zm2-3h2v2H4zM2 4v4h20V4zm4 3H4V5h2zm-4 7h20v-4H2zm2-3h2v2H4z",
  storefront: "M21.9 8.89l-1.05-4.37c-.22-.9-1-1.52-1.91-1.52H5.05c-.9 0-1.69.63-1.9 1.52L2.1 8.89c-.24 1.02-.02 2.06.62 2.88.08.11.19.19.28.29V19c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-6.94c.09-.09.2-.18.28-.28.64-.82.87-1.87.62-2.89zM5.05 5h1.97l-.58 4.86c-.08.65-.6 1.14-1.21 1.14-.49 0-.8-.29-.93-.47-.27-.32-.36-.75-.26-1.17L5.05 5zm4.45.16L9.04 5H11v4.69c0 .72-.55 1.31-1.29 1.31-.34 0-.65-.15-.89-.41-.25-.29-.37-.68-.33-1.07zM13 5h1.96l.51 4.52c.05.39-.07.78-.33 1.07-.22.26-.54.41-.95.41-.67 0-1.22-.59-1.22-1.31V5zm4.96 4.86L17.97 5h1.96l1.05 4.37c.1.42.01.84-.25 1.17-.14.18-.44.47-.94.47-.61 0-1.14-.49-1.21-1.14zm.04 5.83c0 .72-.55 1.31-1.29 1.31-.34 0-.65-.15-.89-.41-.25-.29-.37-.68-.33-1.07l.53-4.69H19.1l-.58 4.86c-.08.65-.6 1.14-1.21 1.14-.49 0-.8-.29-.93-.47-.27-.32-.36-.75-.26-1.17z",
};

// --- Pre-built Path2D objects for each icon (24x24 viewBox paths) ---

const iconPath2DCache = new Map<NodeIcon, Path2D>();

function getIconPath2D(icon: NodeIcon): Path2D | null {
  if (icon === "bullet") return null;
  const cached = iconPath2DCache.get(icon);
  if (cached) return cached;
  const svgPath = ICON_PATHS[icon];
  if (!svgPath) return null;
  const p = new Path2D(svgPath);
  iconPath2DCache.set(icon, p);
  return p;
}

// --- Canvas dimensions ---

export const NODE_SIZE = 10;
// Icon is sized relative to the node circle (world coordinates).
// SVG paths are in a 24×24 viewBox; we scale them to fit within the circle.
const ICON_SCALE_FACTOR = (NODE_SIZE * 2 * 0.7) / 24; // 70% of diameter, in world units

// --- Force-graph data model ---

export interface FGNode {
  id: string;
  label: string;
  displayName: string;
  color: string;
  icon: NodeIcon;
  isFocus: boolean;
  properties: Record<string, string | number | boolean | null>;
}

export interface FGLink {
  id: string;
  source: string;
  target: string;
  type: string;
  properties: Record<string, string | number | boolean | null>;
}

export interface FGGraphData {
  nodes: FGNode[];
  links: FGLink[];
}

export type SelectedItem =
  | { kind: "node"; data: FGNode }
  | { kind: "edge"; data: FGLink };

// --- Labels and relationship types hidden from the interactive graph ---

const HIDDEN_LABELS = new Set(["SourceSystem", "Entity"]);
const HIDDEN_REL_TYPES = new Set([
  "FROM_SOURCE", "SOLD_THROUGH", "OPERATED_BY", "SOLD_BY",
]);

// --- Display name logic ---

function displayNameForNode(node: GraphNode): string {
  const p = node.properties;
  switch (node.label) {
    case "Person":
      return (p["preferred_full_name"] as string | null) ?? (p["person_id"] as string | null) ?? node.id;
    case "Identifier": {
      const idType = p["identifier_type"] as string | null;
      const val = p["normalized_value"] as string | null;
      return idType && val ? `${idType}: ${val}` : node.id;
    }
    case "Address":
      return (p["normalized_full"] as string | null) ?? node.id;
    case "SourceRecord":
      return (p["source_record_id"] as string | null) ?? node.id;
    case "SourceSystem":
      return (p["source_key"] as string | null) ?? node.id;
    case "Order": {
      const orderNo = (p["order_no"] as string | null) ?? (p["source_order_id"] as string | null);
      return orderNo ? `Invoice #${orderNo}` : node.id;
    }
    case "Product":
      return (p["display_name"] as string | null) ?? (p["name"] as string | null) ?? (p["sku"] as string | null) ?? node.id;
    case "LineItem": {
      const lineNo = p["line_no"] as number | null;
      return lineNo != null ? `Line #${String(lineNo)}` : node.id;
    }
    case "Entity":
      return (p["entity_name"] as string | null) ?? (p["entity_key"] as string | null) ?? node.id;
    default:
      return node.id;
  }
}

// --- Canvas rendering callbacks for ForceGraph2D ---

type AnyNode = Record<string, unknown>;

export function paintNode(raw: AnyNode, ctx: CanvasRenderingContext2D, globalScale: number): void {
  const node = raw as unknown as FGNode & { x?: number; y?: number };
  const fontSize = 12 / globalScale;
  const x = node.x ?? 0;
  const y = node.y ?? 0;
  const icon = node.icon ?? "bullet";

  // Focus ring
  if (node.isFocus) {
    ctx.beginPath();
    ctx.arc(x, y, NODE_SIZE + 4, 0, 2 * Math.PI);
    ctx.strokeStyle = "#ff9800";
    ctx.lineWidth = 2.5 / globalScale;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x, y, NODE_SIZE + 7, 0, 2 * Math.PI);
    ctx.strokeStyle = "rgba(255, 152, 0, 0.3)";
    ctx.lineWidth = 3 / globalScale;
    ctx.stroke();
  }

  // Background circle (filled with node color)
  ctx.beginPath();
  ctx.arc(x, y, NODE_SIZE, 0, 2 * Math.PI);
  ctx.fillStyle = node.color;
  ctx.fill();
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 1.5 / globalScale;
  ctx.stroke();

  // Draw icon using Path2D inside the node circle (world coordinates — scales with zoom)
  if (icon !== "bullet") {
    const path2d = getIconPath2D(icon);
    if (path2d) {
      ctx.save();
      // SVG paths are in 24×24 space. Scale to fit inside circle at world size.
      // No globalScale division — the icon lives in world coords like the circle.
      const s = ICON_SCALE_FACTOR;
      ctx.translate(x - 12 * s, y - 12 * s);
      ctx.scale(s, s);
      ctx.fillStyle = "#fff";
      ctx.fill(path2d);
      ctx.restore();
    }
  }

  // Label below
  ctx.font = `${fontSize}px Inter, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillStyle = "#333";
  const text = node.displayName.length > 24 ? node.displayName.slice(0, 22) + "..." : node.displayName;
  ctx.fillText(text, x, y + NODE_SIZE + 4);
}

export function paintNodePointerArea(raw: AnyNode, color: string, ctx: CanvasRenderingContext2D): void {
  const node = raw as unknown as FGNode & { x?: number; y?: number };
  ctx.beginPath();
  ctx.arc(node.x ?? 0, node.y ?? 0, NODE_SIZE + 2, 0, 2 * Math.PI);
  ctx.fillStyle = color;
  ctx.fill();
}

// --- Data conversion ---

export function toForceGraphData(
  graph: PersonGraph,
  focusPersonId?: string,
  focusElementId?: string,
): FGGraphData {
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
        icon: iconForLabel(n.label),
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