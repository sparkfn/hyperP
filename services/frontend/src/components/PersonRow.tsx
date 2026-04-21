"use client";

import { type MouseEvent, type ReactElement } from "react";

import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import IconButton from "@mui/material/IconButton";
import Stack from "@mui/material/Stack";
import TableCell from "@mui/material/TableCell";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import AccountTreeIcon from "@mui/icons-material/AccountTree";

import type { ListedPerson, PersonConnection, SourceRecord } from "@/lib/api-types";
import { confidenceColor, statusColor } from "@/lib/display";
import CountCardsCell, { type CountCardItem } from "@/components/CountCardsCell";

interface PersonRowProps {
  person: ListedPerson;
  selected: boolean;
  onToggleSelect: () => void;
  onRowClick: () => void;
  onContextMenu: (event: MouseEvent<HTMLTableRowElement>) => void;
  onOpenGraph: () => void;
  connections: PersonConnection[] | undefined;
  connectionsLoading: boolean;
  onRequestConnections: () => void;
  sources: SourceRecord[] | undefined;
  sourcesLoading: boolean;
  onRequestSources: () => void;
}

export default function PersonRow({
  person,
  selected,
  onToggleSelect,
  onRowClick,
  onContextMenu,
  onOpenGraph,
  connections,
  connectionsLoading,
  onRequestConnections,
  sources,
  sourcesLoading,
  onRequestSources,
}: PersonRowProps): ReactElement {
  const p: ListedPerson = person;
  return (
    <TableRow
      hover
      sx={{ cursor: "pointer" }}
      onClick={onRowClick}
      onContextMenu={onContextMenu}
      selected={selected}
    >
      <TableCell padding="checkbox" onClick={(e) => e.stopPropagation()}>
        <Checkbox size="small" checked={selected} onChange={onToggleSelect} />
      </TableCell>
      <TableCell>
        <Stack direction="row" spacing={0.5} alignItems="center">
          <Typography variant="body2" sx={{ fontWeight: 500 }}>
            {p.preferred_full_name ?? p.person_id}
          </Typography>
          {p.is_high_value ? (
            <Chip label="HV" color="primary" sx={{ height: 16, fontSize: "0.65rem" }} />
          ) : null}
          {p.is_high_risk ? (
            <Chip label="HR" color="error" sx={{ height: 16, fontSize: "0.65rem" }} />
          ) : null}
        </Stack>
      </TableCell>
      <TableCell>
        <Chip label={p.status} color={statusColor(p.status)} />
      </TableCell>
      <TableCell>{p.preferred_phone ?? "\u2014"}</TableCell>
      <TableCell align="right">
        <Tooltip title={`Profile completeness: ${(p.profile_completeness_score * 100).toFixed(0)}%`}>
          <Chip
            label={`${Math.round(p.profile_completeness_score * 100)}%`}
            color={confidenceColor(p.profile_completeness_score)}
            variant="outlined"
            sx={{ minWidth: 42, height: 18, fontSize: "0.7rem" }}
          />
        </Tooltip>
      </TableCell>
      <TableCell>{p.preferred_email ?? "\u2014"}</TableCell>
      <TableCell>{p.preferred_dob ?? "\u2014"}</TableCell>
      <TableCell
        sx={{
          maxWidth: 220,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        <Tooltip title={p.preferred_address?.normalized_full ?? ""}>
          <span>{p.preferred_address?.normalized_full ?? "\u2014"}</span>
        </Tooltip>
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={p.connection_count}
          label="links"
          emptyText="No connections"
          loading={connectionsLoading}
          items={connectionsToItems(connections)}
          onOpen={onRequestConnections}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={p.source_record_count}
          label="records"
          emptyText="No source records"
          loading={sourcesLoading}
          items={sourcesToItems(sources)}
          onOpen={onRequestSources}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={p.entities.length}
          label="entities"
          emptyText="No entity links"
          items={p.entities.map((e) => ({
            primary: e.display_name ?? e.entity_key,
            secondary: `${e.source_record_count} records`,
            color: "info",
          }))}
        />
      </TableCell>
      <TableCell>{formatDate(p.updated_at)}</TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <Tooltip title="Open graph">
          <IconButton onClick={onOpenGraph}>
            <AccountTreeIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </TableCell>
    </TableRow>
  );
}

function connectionsToItems(data: PersonConnection[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((c) => ({
    primary: c.preferred_full_name ?? c.person_id,
    secondary: describeConnection(c),
    color: "info",
  }));
}

function describeConnection(c: PersonConnection): string {
  const parts: string[] = [];
  for (const si of c.shared_identifiers) {
    parts.push(`${si.identifier_type}:${si.normalized_value}`);
  }
  for (const sa of c.shared_addresses) {
    parts.push(`address:${sa.normalized_full ?? sa.address_id}`);
  }
  for (const kr of c.knows_relationships) {
    parts.push(`knows:${kr.relationship_label ?? kr.relationship_category}`);
  }
  return parts.join(" · ");
}

function sourcesToItems(data: SourceRecord[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((r) => ({
    primary: r.source_system,
    secondary: `${r.source_record_id} · ${r.record_type} · ${formatDate(r.ingested_at)}`,
    color: r.record_type === "conversation" ? "warning" : "default",
  }));
}

function formatDate(value: string): string {
  if (!value) return "\u2014";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().slice(0, 10);
}
