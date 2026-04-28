import type { ReactElement } from "react";
import { notFound } from "next/navigation";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { UpstreamError, apiFetch } from "@/lib/api-server";
import type { Person, PersonConnection, SalesOrder } from "@/lib/api-types";
import type { PersonIdentifier, PersonSourceRecord } from "@/lib/api-types-person";
import { statusColor } from "@/lib/display";
import {
  ConnectionsSection,
  IdentifiersSection,
  SourceRecordsSection,
} from "./PublicPersonSections";
import PublicSalesTable from "./PublicSalesTable";

interface PageProps {
  params: Promise<{ token: string }>;
}

async function fetchPublic<T>(token: string, path: string): Promise<T[]> {
  try {
    const res = await apiFetch<T[]>(`/public/persons/${encodeURIComponent(token)}${path}`, {
      authToken: null,
    });
    return res.data;
  } catch {
    return [];
  }
}

export default async function PublicPersonPage({ params }: PageProps): Promise<ReactElement> {
  const { token } = await params;

  let person: Person;
  try {
    const res = await apiFetch<Person>(`/public/persons/${encodeURIComponent(token)}`, {
      authToken: null,
    });
    person = res.data;
  } catch (err: unknown) {
    if (err instanceof UpstreamError && err.status === 404) notFound();
    throw err;
  }

  const [identifiers, connections, sourceRecords, sales] = await Promise.all([
    fetchPublic<PersonIdentifier>(token, "/identifiers"),
    fetchPublic<PersonConnection>(token, "/connections"),
    fetchPublic<PersonSourceRecord>(token, "/source-records"),
    fetchPublic<SalesOrder>(token, "/sales"),
  ]);

  return (
    <Box sx={{ p: { xs: 2, sm: 4 } }}>
      <Stack spacing={3}>
        <Typography variant="caption" color="text.secondary">
          Public profile — read only
        </Typography>
        <ProfileHeader person={person} />
        <ConnectionsSection connections={connections} />
        <IdentifiersSection identifiers={identifiers} />
        <SourceRecordsSection sourceRecords={sourceRecords} />
        <PublicSalesTable sales={sales} />
      </Stack>
    </Box>
  );
}

function ProfileHeader({ person }: { person: Person }): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 3 }}>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h5" fontWeight={600}>
          {person.preferred_full_name ?? person.person_id}
        </Typography>
        <Chip label={person.status} size="small" color={statusColor(person.status)} />
        {person.is_high_value ? <Chip label="high value" size="small" color="primary" /> : null}
        {person.is_high_risk ? <Chip label="high risk" size="small" color="error" /> : null}
      </Stack>
      <Typography variant="caption" color="text.secondary">
        {person.person_id}
      </Typography>
      <Divider sx={{ my: 2 }} />
      <Grid container spacing={2}>
        <ProfileField label="NRIC" value={person.preferred_nric} mono />
        <ProfileField label="Date of Birth" value={person.preferred_dob} />
        <ProfileField label="Phone" value={person.preferred_phone} />
        <ProfileField label="Email" value={person.preferred_email} />
        <ProfileField label="Address" value={person.preferred_address?.normalized_full ?? null} />
        <ProfileField
          label="Profile Completeness"
          value={`${(person.profile_completeness_score * 100).toFixed(0)}%`}
        />
        <ProfileField label="Source Records" value={String(person.source_record_count)} />
        <ProfileField label="Updated" value={person.updated_at} />
      </Grid>
    </Paper>
  );
}

interface FieldProps {
  label: string;
  value: string | null;
  mono?: boolean;
}

function ProfileField({ label, value, mono = false }: FieldProps): ReactElement {
  return (
    <Grid size={{ xs: 12, sm: 6, md: 3 }}>
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
