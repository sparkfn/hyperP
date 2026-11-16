"use client";

import { use, type ReactElement } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import Alert from "@mui/material/Alert";
import Chip from "@mui/material/Chip";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ProfileDetail } from "@/lib/api-types-person";
import { formatDate } from "@/lib/display";

interface PageProps {
  params: Promise<{ profileId: string }>;
}

export default function ProfileDetailPage({ params }: PageProps): ReactElement {
  const { profileId } = use(params);
  const query = useQuery({
    queryKey: ["profile", profileId],
    queryFn: async ({ signal }): Promise<ProfileDetail> => {
      const envelope = await bffFetchEnvelope<ProfileDetail>(
        `/bff/profiles/${encodeURIComponent(profileId)}`,
        { signal },
      );
      return envelope.data;
    },
  });

  if (query.isPending) {
    return <Typography sx={{ p: 3 }}>Loading profile…</Typography>;
  }
  if (!query.isSuccess) {
    const notFound = query.error instanceof BffError && query.error.status === 404;
    return (
      <Alert severity={notFound ? "warning" : "error"} sx={{ m: 3 }}>
        {notFound ? "Profile not found." : "Failed to load profile."}
      </Alert>
    );
  }
  const profile = query.data;

  return (
    <Stack spacing={2} sx={{ p: 3 }}>
      <Stack direction="row" spacing={1} alignItems="center">
        <Typography variant="h5">{profile.profile_id}</Typography>
        <Chip
          label={profile.status}
          size="small"
          color={profile.status === "open" ? "success" : profile.status === "promoted" ? "default" : "error"}
        />
        <Chip label="Unverified" size="small" color="warning" />
      </Stack>
      <Typography variant="body2" color="text.secondary">
        Materialized {formatDate(profile.materialized_at)} · evidence version{" "}
        {profile.evidence_version}
      </Typography>
      {profile.promoted_person_id !== null ? (
        <Typography variant="body2">
          Promoted to{" "}
          <Link href={`/persons/${encodeURIComponent(profile.promoted_person_id)}`}>
            {profile.promoted_person_id}
          </Link>
        </Typography>
      ) : null}

      <Paper variant="outlined">
        <Typography variant="subtitle1" sx={{ px: 2, pt: 1 }}>
          Identifiers
        </Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Type</TableCell>
              <TableCell>Value</TableCell>
              <TableCell>Verified</TableCell>
              <TableCell>Active</TableCell>
              <TableCell>Last confirmed</TableCell>
              <TableCell>Provisional</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {profile.identifiers.map((identifier) => (
              <TableRow key={`${identifier.identifier_type}:${identifier.normalized_value}`}>
                <TableCell>{identifier.identifier_type}</TableCell>
                <TableCell>{identifier.normalized_value}</TableCell>
                <TableCell>{identifier.is_verified ? "Yes" : "No"}</TableCell>
                <TableCell>{identifier.is_active ? "Yes" : "No"}</TableCell>
                <TableCell>{formatDate(identifier.last_confirmed_at)}</TableCell>
                <TableCell>
                  {identifier.score != null
                    ? `Score ${identifier.score} (provisional)`
                    : "—"}
                  {identifier.golden_rank != null ? ` · rank ${identifier.golden_rank}` : ""}
                </TableCell>
              </TableRow>
            ))}
            {profile.identifiers.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6}>No identifiers.</TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </Paper>

      <Paper variant="outlined">
        <Typography variant="subtitle1" sx={{ px: 2, pt: 1 }}>
          Member source records
        </Typography>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Record</TableCell>
              <TableCell>Type</TableCell>
              <TableCell>Source</TableCell>
              <TableCell>Lifecycle</TableCell>
              <TableCell>Link</TableCell>
              <TableCell>Observed</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {profile.source_records.map((record) => (
              <TableRow key={record.source_record_pk}>
                <TableCell>{record.source_record_id}</TableCell>
                <TableCell>{record.record_type}</TableCell>
                <TableCell>{record.source_system}</TableCell>
                <TableCell>{record.lifecycle_status}</TableCell>
                <TableCell>{record.link_status}</TableCell>
                <TableCell>{formatDate(record.observed_at)}</TableCell>
              </TableRow>
            ))}
            {profile.source_records.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6}>No member records.</TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </Paper>
    </Stack>
  );
}
