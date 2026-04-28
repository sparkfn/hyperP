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

import type { ListedPerson, PersonConnection, SalesOrder, SourceRecord } from "@/lib/api-types";
import type { PersonIdentifier } from "@/lib/api-types-person";
import { confidenceColor, connectionsToItems, formatDate, formatDob, identifiersToItems, ordersToItems, sourcesToItems } from "@/lib/display";
import CountCardsCell from "@/components/CountCardsCell";

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
  identifiers: PersonIdentifier[] | undefined;
  identifiersLoading: boolean;
  onRequestIdentifiers: () => void;
  orders: SalesOrder[] | undefined;
  ordersLoading: boolean;
  onRequestOrders: () => void;
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
  identifiers,
  identifiersLoading,
  onRequestIdentifiers,
  orders,
  ordersLoading,
  onRequestOrders,
}: PersonRowProps): ReactElement {
  return (
    <TableRow
      hover
      sx={{ cursor: "pointer" }}
      onClick={onRowClick}
      onContextMenu={onContextMenu}
      selected={selected}
    >
      <TableCell padding="checkbox" onClick={(e) => e.stopPropagation()} sx={{ position: "sticky", left: 0, zIndex: 2, bgcolor: "background.paper" }}>
        <Checkbox size="small" checked={selected} onChange={onToggleSelect} />
      </TableCell>
      <TableCell>
        <Stack direction="row" spacing={0.5} alignItems="center">
          <Typography variant="body2" sx={{ fontWeight: 500 }}>
            {person.preferred_full_name ?? person.person_id}
          </Typography>
          {person.is_high_value && <Chip label="HV" color="primary" sx={{ height: 16, fontSize: "0.65rem" }} />}
          {person.is_high_risk && <Chip label="HR" color="error" sx={{ height: 16, fontSize: "0.65rem" }} />}
        </Stack>
      </TableCell>
      <TableCell>{person.preferred_phone ?? "—"}</TableCell>
      <TableCell align="right">
        <Tooltip title={`Profile completeness: ${Math.round(person.profile_completeness_score * 100)}%`}>
          <Chip label={`${Math.round(person.profile_completeness_score * 100)}%`} color={confidenceColor(person.profile_completeness_score)} variant="outlined" sx={{ minWidth: 42, height: 18, fontSize: "0.7rem" }} />
        </Tooltip>
      </TableCell>
      <TableCell>{person.preferred_email ?? "—"}</TableCell>
      <TableCell sx={{ whiteSpace: "nowrap" }}>{formatDob(person.preferred_dob ?? null)}</TableCell>
      <TableCell sx={{ maxWidth: 160, fontFamily: "monospace", fontSize: "0.72rem", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        <Tooltip title={person.preferred_nric ?? ""}>
          <span>{person.preferred_nric ?? "—"}</span>
        </Tooltip>
      </TableCell>
      <TableCell sx={{ maxWidth: 220, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        <Tooltip title={person.preferred_address?.normalized_full ?? ""}>
          <span>{person.preferred_address?.normalized_full ?? "—"}</span>
        </Tooltip>
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.connection_count}
          label="links"
          emptyText="No connections"
          loading={connectionsLoading}
          items={connectionsToItems(connections)}
          onOpen={onRequestConnections}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.order_count}
          label="orders"
          emptyText="No orders"
          loading={ordersLoading}
          items={ordersToItems(orders)}
          onOpen={onRequestOrders}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.source_record_count}
          label="records"
          emptyText="No source records"
          loading={sourcesLoading}
          items={sourcesToItems(sources)}
          onOpen={onRequestSources}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.identifier_count}
          label="identifiers"
          emptyText="No identifiers"
          loading={identifiersLoading}
          items={identifiersToItems(identifiers)}
          onOpen={onRequestIdentifiers}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.entity_count}
          label="entities"
          emptyText="No entity links"
          items={person.entities.map((e) => ({
            primary: e.display_name ?? e.entity_key,
            secondary: `${e.source_record_count} records`,
            color: "info",
          }))}
        />
      </TableCell>
      <TableCell sx={{ whiteSpace: "nowrap" }}>{formatDate(person.updated_at)}</TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()} sx={{ position: "sticky", right: 0, zIndex: 2, bgcolor: "background.paper" }}>
        <Tooltip title="Open graph">
          <IconButton onClick={onOpenGraph}>
            <AccountTreeIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </TableCell>
    </TableRow>
  );
}

