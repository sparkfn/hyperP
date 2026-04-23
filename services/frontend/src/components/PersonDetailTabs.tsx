"use client";

import { useState, type ReactElement, type SyntheticEvent } from "react";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import type { Person } from "@/lib/api-types";
import { statusColor } from "@/lib/display";
import AuditTab from "./AuditTab";
import ConnectionsCard from "./ConnectionsCard";
import Gate from "./auth/Gate";
import IdentifiersSection from "./IdentifiersSection";
import ManualMergeDialog from "./ManualMergeDialog";
import MatchesTab from "./MatchesTab";
import PersonFocusedGraph from "./PersonFocusedGraph";
import PersonSection from "./PersonSection";
import SalesCard from "./SalesCard";
import SourceRecordsTab from "./SourceRecordsTab";
import SurvivorshipOverrideDialog from "./SurvivorshipOverrideDialog";

interface Props {
  person: Person;
}

export default function PersonDetailTabs({ person }: Props): ReactElement {
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
        <Tab label="Matches" />
      </Tabs>

      {tab === 0 ? (
        <Stack spacing={2}>
          <PersonHeader
            person={person}
            onMergeClick={() => setMergeOpen(true)}
            onOverrideClick={() => setOverrideOpen(true)}
          />
          <ConnectionsCard personId={person.person_id} />
          <PersonSection title="Identifiers">
            <IdentifiersSection personId={person.person_id} />
          </PersonSection>
          <PersonSection title="Source Records">
            <SourceRecordsTab personId={person.person_id} />
          </PersonSection>
          <SalesCard personId={person.person_id} />
          <PersonSection title="Audit">
            <AuditTab personId={person.person_id} />
          </PersonSection>
          <PersonGraphCard person={person} />
        </Stack>
      ) : null}
      {tab === 1 ? <MatchesTab personId={person.person_id} /> : null}

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
    <Paper variant="outlined" sx={{ p: 2 }}>
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
          <Gate mode="admin">
            <Button size="small" variant="outlined" onClick={onOverrideClick}>
              Override field
            </Button>
          </Gate>
          <Gate mode="admin">
            <Button size="small" variant="contained" onClick={onMergeClick}>
              Merge into…
            </Button>
          </Gate>
        </Stack>
      </Stack>

      <Typography variant="caption" color="text.secondary">
        {person.person_id}
      </Typography>

      <Divider sx={{ my: 2 }} />

      <Grid container spacing={2}>
        <Field label="NRIC" value={person.preferred_nric} mono />
        <Field label="Date of Birth" value={person.preferred_dob} />
        <Field label="Phone" value={person.preferred_phone} />
        <Field label="Email" value={person.preferred_email} />
        <Field label="Address" value={person.preferred_address?.normalized_full ?? null} />
        <Field
          label="Profile Completeness"
          value={`${(person.profile_completeness_score * 100).toFixed(0)}%`}
        />
        <Field label="Source Records" value={String(person.source_record_count)} />
        <Field label="Updated" value={person.updated_at} />
      </Grid>
    </Paper>
  );
}

function PersonGraphCard({ person }: { person: Person }): ReactElement {
  const title = person.preferred_full_name ?? person.person_id;
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Graph
      </Typography>
      <Box sx={{ height: 520 }}>
        <PersonFocusedGraph
          initialPersonId={person.person_id}
          initialTitle={title}
          height="100%"
        />
      </Box>
    </Paper>
  );
}

interface FieldProps {
  label: string;
  value: string | null;
  cols?: 1 | 2 | 3;
  mono?: boolean;
}

function Field({ label, value, cols = 1, mono = false }: FieldProps): ReactElement {
  const mdSpan = cols === 3 ? 12 : cols === 2 ? 6 : 3;
  const smSpan = cols === 1 ? 6 : 12;
  return (
    <Grid size={{ xs: 12, sm: smSpan, md: mdSpan }}>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Tooltip title={value ?? ""} enterDelay={400} enterTouchDelay={500}>
        <Typography
          variant="body2"
          fontFamily={mono ? "monospace" : undefined}
          sx={{
            display: "-webkit-box",
            WebkitLineClamp: 3,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
            wordBreak: "break-word",
          }}
        >
          {value ?? "—"}
        </Typography>
      </Tooltip>
    </Grid>
  );
}
