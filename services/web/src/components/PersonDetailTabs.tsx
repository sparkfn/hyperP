"use client";

import { useState, type ReactElement, type SyntheticEvent } from "react";
import Link from "next/link";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tabs from "@mui/material/Tabs";
import Typography from "@mui/material/Typography";

import type { Person, PersonConnection } from "@/lib/api-types";
import { statusColor } from "@/lib/display";

import AuditTab from "./AuditTab";
import ManualMergeDialog from "./ManualMergeDialog";
import MatchesTab from "./MatchesTab";
import SourceRecordsTab from "./SourceRecordsTab";
import SurvivorshipOverrideDialog from "./SurvivorshipOverrideDialog";

interface Props {
  person: Person;
  connections: PersonConnection[];
}

export default function PersonDetailTabs({ person, connections }: Props): ReactElement {
  const [tab, setTab] = useState<number>(0);
  const [mergeOpen, setMergeOpen] = useState<boolean>(false);
  const [overrideOpen, setOverrideOpen] = useState<boolean>(false);

  const handleChange = (_e: SyntheticEvent, value: number): void => {
    setTab(value);
  };

  return (
    <Box>
      <Tabs value={tab} onChange={handleChange} sx={{ mb: 2 }}>
        <Tab label="Profile" />
        <Tab label="Source Records" />
        <Tab label="Audit" />
        <Tab label="Matches" />
      </Tabs>

      {tab === 0 ? (
        <Stack spacing={3}>
          <PersonHeader
            person={person}
            onMergeClick={() => setMergeOpen(true)}
            onOverrideClick={() => setOverrideOpen(true)}
          />
          <ConnectionsCard connections={connections} />
        </Stack>
      ) : null}
      {tab === 1 ? <SourceRecordsTab personId={person.person_id} /> : null}
      {tab === 2 ? <AuditTab personId={person.person_id} /> : null}
      {tab === 3 ? <MatchesTab personId={person.person_id} /> : null}

      <ManualMergeDialog
        open={mergeOpen}
        fromPersonId={person.person_id}
        onClose={() => setMergeOpen(false)}
      />
      <SurvivorshipOverrideDialog
        open={overrideOpen}
        personId={person.person_id}
        onClose={() => setOverrideOpen(false)}
      />
    </Box>
  );
}

interface HeaderProps {
  person: Person;
  onMergeClick: () => void;
  onOverrideClick: () => void;
}

function PersonHeader({ person, onMergeClick, onOverrideClick }: HeaderProps): ReactElement {
  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Stack
        direction="row"
        spacing={2}
        alignItems="center"
        justifyContent="space-between"
        sx={{ mb: 2 }}
      >
        <Stack direction="row" spacing={2} alignItems="center">
          <Typography variant="h5" fontWeight={600}>
            {person.preferred_full_name ?? person.person_id}
          </Typography>
          <Chip label={person.status} size="small" color={statusColor(person.status)} />
          {person.is_high_value ? <Chip label="high value" size="small" color="primary" /> : null}
          {person.is_high_risk ? <Chip label="high risk" size="small" color="error" /> : null}
        </Stack>
        <Stack direction="row" spacing={1}>
          <Button size="small" variant="outlined" onClick={onOverrideClick}>
            Override field
          </Button>
          <Button size="small" variant="contained" onClick={onMergeClick}>
            Merge into…
          </Button>
        </Stack>
      </Stack>

      <Typography variant="caption" color="text.secondary">
        {person.person_id}
      </Typography>

      <Divider sx={{ my: 2 }} />

      <Grid container spacing={2}>
        <Field label="Phone" value={person.preferred_phone} />
        <Field label="Email" value={person.preferred_email} />
        <Field label="Date of Birth" value={person.preferred_dob} />
        <Field
          label="Profile Completeness"
          value={`${(person.profile_completeness_score * 100).toFixed(0)}%`}
        />
        <Field label="Source Records" value={String(person.source_record_count)} />
        <Field label="Updated" value={person.updated_at} />
        <Field label="Address" value={person.preferred_address?.normalized_full ?? null} full />
      </Grid>
    </Paper>
  );
}

function ConnectionsCard({ connections }: { connections: PersonConnection[] }): ReactElement {
  return (
    <Paper elevation={0} variant="outlined" sx={{ p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Connections
      </Typography>
      {connections.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No connected persons via shared identifiers or addresses.
        </Typography>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Name</TableCell>
              <TableCell>Status</TableCell>
              <TableCell align="right">Hops</TableCell>
              <TableCell>Shared identifiers</TableCell>
              <TableCell>Shared addresses</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {connections.map((c) => (
              <TableRow key={c.person_id} hover>
                <TableCell>
                  <Link href={`/persons/${c.person_id}`} style={{ textDecoration: "none" }}>
                    {c.preferred_full_name ?? c.person_id}
                  </Link>
                </TableCell>
                <TableCell>{c.status}</TableCell>
                <TableCell align="right">{c.hops}</TableCell>
                <TableCell>
                  {c.shared_identifiers
                    .map((s) => `${s.identifier_type}:${s.normalized_value}`)
                    .join(", ") || "—"}
                </TableCell>
                <TableCell>
                  {c.shared_addresses.map((a) => a.normalized_full ?? a.address_id).join(", ") ||
                    "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Paper>
  );
}

interface FieldProps {
  label: string;
  value: string | null;
  full?: boolean;
}

function Field({ label, value, full = false }: FieldProps): ReactElement {
  return (
    <Grid size={{ xs: 12, sm: full ? 12 : 6, md: full ? 12 : 4 }}>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="body2">{value ?? "—"}</Typography>
    </Grid>
  );
}
