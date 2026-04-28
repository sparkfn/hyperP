import type { ReactElement } from "react";

import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import type { PersonConnection } from "@/lib/api-types";
import type { PersonIdentifier, PersonSourceRecord } from "@/lib/api-types-person";

export function ConnectionsSection({
  connections,
}: {
  connections: PersonConnection[];
}): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Connections
      </Typography>
      {connections.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No connected persons via shared identifiers, addresses, or relationships.
        </Typography>
      ) : (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Name</TableCell>
                <TableCell>Status</TableCell>
                <TableCell align="right">Hops</TableCell>
                <TableCell>Shared identifiers</TableCell>
                <TableCell>Shared addresses</TableCell>
                <TableCell>Relationships</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {connections.map((c) => (
                <TableRow key={c.person_id}>
                  <TableCell>{c.preferred_full_name ?? c.person_id}</TableCell>
                  <TableCell>{c.status}</TableCell>
                  <TableCell align="right">{c.hops}</TableCell>
                  <TableCell>
                    {c.shared_identifiers
                      .map((s) => `${s.identifier_type}: ${s.normalized_value}`)
                      .join(", ") || "—"}
                  </TableCell>
                  <TableCell>
                    {c.shared_addresses
                      .map((a) => a.normalized_full ?? a.address_id)
                      .join(", ") || "—"}
                  </TableCell>
                  <TableCell>
                    {c.knows_relationships
                      .map((k) => k.relationship_label ?? k.relationship_category)
                      .join(", ") || "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
}

export function IdentifiersSection({
  identifiers,
}: {
  identifiers: PersonIdentifier[];
}): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Identifiers
      </Typography>
      {identifiers.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No identifiers linked to this person.
        </Typography>
      ) : (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Type</TableCell>
                <TableCell>Value</TableCell>
                <TableCell>Active</TableCell>
                <TableCell>Verified</TableCell>
                <TableCell>Source system</TableCell>
                <TableCell>Last confirmed</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {identifiers.map((id) => (
                <TableRow key={`${id.identifier_type}:${id.normalized_value}`}>
                  <TableCell>{id.identifier_type}</TableCell>
                  <TableCell>
                    <Tooltip title={id.normalized_value}>
                      <Typography
                        variant="body2"
                        fontFamily="monospace"
                        sx={{
                          maxWidth: 220,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {id.normalized_value}
                      </Typography>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={id.is_active ? "active" : "inactive"}
                      size="small"
                      color={id.is_active ? "success" : "default"}
                    />
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={id.is_verified ? "verified" : "unverified"}
                      size="small"
                      color={id.is_verified ? "success" : "default"}
                      variant="outlined"
                    />
                  </TableCell>
                  <TableCell>{id.source_system_key ?? "—"}</TableCell>
                  <TableCell>{id.last_confirmed_at?.slice(0, 10) ?? "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
}

export function SourceRecordsSection({
  sourceRecords,
}: {
  sourceRecords: PersonSourceRecord[];
}): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Source Records
      </Typography>
      {sourceRecords.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No source records linked to this person.
        </Typography>
      ) : (
        <TableContainer>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Source system</TableCell>
                <TableCell>Record ID</TableCell>
                <TableCell>Type</TableCell>
                <TableCell>Link status</TableCell>
                <TableCell>Observed</TableCell>
                <TableCell>Ingested</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sourceRecords.map((sr) => (
                <TableRow key={sr.source_record_pk}>
                  <TableCell>{sr.source_system}</TableCell>
                  <TableCell>
                    <Typography variant="body2" fontFamily="monospace">
                      {sr.source_record_id}
                    </Typography>
                  </TableCell>
                  <TableCell>{sr.record_type}</TableCell>
                  <TableCell>{sr.link_status}</TableCell>
                  <TableCell>{sr.observed_at.slice(0, 10)}</TableCell>
                  <TableCell>{sr.ingested_at.slice(0, 10)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </Paper>
  );
}
