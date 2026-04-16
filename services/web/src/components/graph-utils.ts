/**
 * Shared types, colors, and data-conversion logic for the interactive
 * graph viewer. Extracted from PersonGraphViewer to keep module sizes
 * within project limits.
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

// --- Force-graph data model ---

export interface FGNode {
  id: string;
  label: string;
  displayName: string;
  color: string;
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
