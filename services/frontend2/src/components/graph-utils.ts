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
  BankruptcyCase: "#b45309",
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
  | "gavel"
  | "storefront";

const LABEL_ICONS: Record<string, NodeIcon> = {
  Person: "person",
  Identifier: "vpnKey",
  Address: "home",
  SourceRecord: "description",
  BankruptcyCase: "gavel",
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
  receipt: "M18 17H6v-2h12zm3-2.5V16l1 1v3h-2v-1H5v1H3v-3l1-1v-1.5c0-1.38-1.12-2.5-2.5-2.5v-2c1.38 0 2.5-1.12 2.5-2.5V6l-1-1V2h2v1h14V2h2v3l-1 1v1.5c0 1.38 1.12 2.5 2.5 2.5v2c-1.38 0-2.5 1.12-2.5 2.5M21 4H3v2h18zm-3 6H6v2h12z",
  inventory: "M20 2H4c-1 0-2 .9-2 2v3.01c0 .72.44 1.34 1.07 1.69C3.65 8.98 4 9.64 4 10.35s-.35.71-.93 1.07C2.44 11.77 2 12.39 2 13.11V16c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2v-2.89c0-.72-.44-1.34-1.07-1.69-.58-.35-.93-1.01-.93-1.73s.35-1.38.93-1.73c.63-.35 1.07-.97 1.07-1.69V5c0-1.1-.9-3-2-3m0 3v2.01c-1.08.63-2 1.72-2 3.36s.92 2.75 2 3.38V16H4v-2.26c1.08-.63 2-1.72 2-3.36s-.92-2.75-2-3.38V5h16",
  bullet: "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4",
  dataSource: "M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9-4.03-9-9-9m0 16c-3.87 0-7-3.13-7-7s3.13-7 7-7 7 3.13 7 7-3.13 7-7 7m0-12c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5m0 8c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3",
  gavel: "M1 21h12v2H1zM5.245 8.07l2.83-2.827 14.14 14.142-2.828 2.828zM12.317 1l5.657 5.656-2.83 2.83-5.654-5.66zM3.825 9.485l5.657 5.657-2.828 2.828-5.657-5.657z",
  storefront: "M21.9 8.89 20.49 5.1c-.38-.67-1.06-1.1-1.81-1.1H5.32c-.75 0-1.43.43-1.81 1.1L2.1 8.89c-.24.44-.1.98.34 1.22l.39.22c.5.28 1.07.44 1.67.44 1.21 0 2.27-.67 2.83-1.65.56.98 1.62 1.65 2.83 1.65s2.27-.67 2.83-1.65c.56.98 1.62 1.65 2.83 1.65 1.21 0 2.27-.67 2.83-1.65.56.98 1.62 1.65 2.83 1.65.59 0 1.17-.16 1.66-.44l.39-.22c.45-.24.59-.78.35-1.22M19.32 12c-.5 0-1-.08-1.46-.22V17H6.14v-5.22c-.46.14-.96.22-1.46.22-.49 0-.95-.08-1.39-.22V19h17.42v-7.22c-.44.14-.9.22-1.39.22",
};

const ICON_SCALE_FACTOR = (18 * 2) / 24; // icon fills ~18px of 24px box → scale to fit node
export const NODE_SIZE = 12;

// --- Types ---

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

// --- Labels hidden by default ---

const HIDDEN_LABELS = new Set(["IngestRun"]);
const HIDDEN_REL_TYPES = new Set([
  "HAS_FACT",
  "FOR_DECISION",
  "SOURCE_FOR",
]);

// --- Display-name helpers ---

function displayNameForNode(node: GraphNode): string {
  const props = node.properties;
  if (node.label === "Person") {
    const name = (props["preferred_full_name"] as string | null) ?? props["full_name"] as string | null;
    if (name) return name;
    const first = (props["preferred_first_name"] as string | null) ?? (props["first_name"] as string | null);
    const last = (props["preferred_last_name"] as string | null) ?? (props["last_name"] as string | null);
    if (first && last) return `${first} ${last}`;
    return node.id.slice(0, 8);
  }
  if (node.label === "Identifier") {
    const val = (props["value"] as string | null) ?? (props["identifier_value"] as string | null);
    const kind = (props["identifier_type"] as string | null) ?? (props["type"] as string | null);
    if (val && kind) return `${kind}: ${val}`;
    if (val) return val;
    return node.id.slice(0, 8);
  }
  if (node.label === "Address") {
    const full = (props["normalized_full"] as string | null) ?? (props["full_address"] as string | null);
    if (full) return full.length > 30 ? `${full.slice(0, 28)}…` : full;
    return node.id.slice(0, 8);
  }
  if (node.label === "SourceRecord") {
    const name = (props["record_type"] as string | null) ?? (props["source_name"] as string | null);
    if (name) return name;
    return node.id.slice(0, 8);
  }
  const fallback = (props["name"] as string | null) ?? (props["label"] as string | null);
  return fallback ?? node.id.slice(0, 8);
}

// --- Icon Path2D cache ---

const iconPathCache = new Map<NodeIcon, Path2D | null>();

function getIconPath2D(icon: NodeIcon): Path2D | null {
  const cached = iconPathCache.get(icon);
  if (cached !== undefined) return cached;
  const pathData = ICON_PATHS[icon];
  if (!pathData) {
    iconPathCache.set(icon, null);
    return null;
  }
  const p = new Path2D(pathData);
  iconPathCache.set(icon, p);
  return p;
}

function themeValue(name: string, fallback: string): string {
  if (typeof document === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

// --- Canvas painting callbacks ---

export function paintNode(
  raw: Record<string, unknown>,
  ctx: CanvasRenderingContext2D,
  globalScale: number,
): void {
  const node = raw as unknown as FGNode & { x?: number; y?: number };
  const { x, y, color, icon, isFocus } = node;
  if (x === undefined || y === undefined) return;

  const fontSize = Math.max(10 / globalScale, 3);

  // Focus ring
  if (isFocus) {
    ctx.beginPath();
    ctx.arc(x, y, NODE_SIZE + 4, 0, 2 * Math.PI);
    ctx.strokeStyle = "#1565c0";
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
  ctx.fillStyle = color;
  ctx.fill();
  ctx.strokeStyle = themeValue("--bg-card", "#fff");
  ctx.lineWidth = 1.5 / globalScale;
  ctx.stroke();

  // Draw icon using Path2D inside the node circle (world coordinates — scales with zoom)
  if (icon !== "bullet") {
    const path2d = getIconPath2D(icon);
    if (path2d) {
      ctx.save();
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
  ctx.fillStyle = themeValue("--text-secondary", "#333");
  const text = node.displayName.length > 24 ? node.displayName.slice(0, 22) + "…" : node.displayName;
  ctx.fillText(text, x, y + NODE_SIZE + 4);
}

export function paintNodePointerArea(raw: Record<string, unknown>, color: string, ctx: CanvasRenderingContext2D): void {
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