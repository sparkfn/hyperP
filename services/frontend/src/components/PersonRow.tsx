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

import type { ListedPerson, PopoverDisplayItem } from "@/lib/api-types";
import type { PersonBankruptcyCase, PersonSharedIdentifierCandidate } from "@/lib/api-types-person";
import { confidenceColor, formatDate, formatDob } from "@/lib/display";
import CountCardsCell, { type CountCardItem } from "@/components/CountCardsCell";

interface PersonRowProps {
  person: ListedPerson;
  selected: boolean;
  onToggleSelect: () => void;
  onRowClick: () => void;
  onContextMenu: (event: MouseEvent<HTMLTableRowElement>) => void;
  onOpenGraph: () => void;
  connectionsItems: PopoverDisplayItem[] | undefined;
  connectionsLoading: boolean;
  onRequestConnections: () => void;
  sourcesItems: PopoverDisplayItem[] | undefined;
  sourcesLoading: boolean;
  onRequestSources: () => void;
  matches: PersonSharedIdentifierCandidate[] | undefined;
  matchesLoading: boolean;
  onRequestMatches: () => void;
  bankruptcyCases: PersonBankruptcyCase[] | undefined;
  bankruptcyLoading: boolean;
  onRequestBankruptcyCases: () => void;
}

function bankruptcyToItems(cases: PersonBankruptcyCase[] | undefined): CountCardItem[] | undefined {
  return cases?.map((c) => ({
    primary: c.case_number ?? c.source_case_id,
    secondary: [c.event_type, c.event_date].filter(Boolean).join(" · ") || c.document_type,
    color: "warning",
  }));
}

function matchesToItems(matches: PersonSharedIdentifierCandidate[] | undefined): CountCardItem[] | undefined {
  return matches?.map((candidate) => ({
    primary: candidate.preferred_full_name ?? candidate.person_id,
    secondary: candidate.identifiers
      .map((identifier) => `${identifier.identifier_type}:${identifier.normalized_value}`)
      .join(" · "),
    color: candidate.identifier_strength === "strong" ? "warning" : "info",
  }));
}

export default function PersonRow({
  person,
  selected,
  onToggleSelect,
  onRowClick,
  onContextMenu,
  onOpenGraph,
  connectionsItems,
  connectionsLoading,
  onRequestConnections,
  sourcesItems,
  sourcesLoading,
  onRequestSources,
  matches,
  matchesLoading,
  onRequestMatches,
  bankruptcyCases,
  bankruptcyLoading,
  onRequestBankruptcyCases,
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
          label="connections"
          emptyText="No connections"
          loading={connectionsLoading}
          items={connectionsItems}
          onOpen={onRequestConnections}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.order_count}
          label="orders"
          emptyText="Order previews moved to the person detail page"
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.bankruptcy_case_count}
          label="cases"
          emptyText="No bankruptcy cases"
          loading={bankruptcyLoading}
          items={bankruptcyToItems(bankruptcyCases)}
          onOpen={onRequestBankruptcyCases}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.source_record_count}
          label="records"
          emptyText="No source records"
          loading={sourcesLoading}
          items={sourcesItems}
          onOpen={onRequestSources}
        />
      </TableCell>
      <TableCell align="center" onClick={(e) => e.stopPropagation()}>
        <CountCardsCell
          count={person.possible_match_count}
          label="matches"
          emptyText="No possible matches"
          loading={matchesLoading}
          items={matchesToItems(matches)}
          onOpen={onRequestMatches}
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

