import type { ReactElement } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";

import PersonDetailTabs from "@/components/PersonDetailTabs";
import { UpstreamError, apiFetch } from "@/lib/api-server";
import type { Person, PersonConnection } from "@/lib/api-types";

interface PageProps {
  params: Promise<{ personId: string }>;
}

async function loadPerson(personId: string): Promise<Person> {
  try {
    const res = await apiFetch<Person>(`/persons/${encodeURIComponent(personId)}`);
    return res.data;
  } catch (err: unknown) {
    if (err instanceof UpstreamError && err.status === 404) {
      notFound();
    }
    throw err;
  }
}

async function loadConnections(personId: string): Promise<PersonConnection[]> {
  try {
    const res = await apiFetch<PersonConnection[]>(
      `/persons/${encodeURIComponent(personId)}/connections`,
      { query: { connection_type: "all" } },
    );
    return res.data;
  } catch {
    // Connections are best-effort; the page still renders without them.
    return [];
  }
}

export default async function PersonDetailPage({ params }: PageProps): Promise<ReactElement> {
  const { personId } = await params;
  const [person, connections] = await Promise.all([loadPerson(personId), loadConnections(personId)]);

  return (
    <Stack spacing={3}>
      <Box>
        <Button component={Link} href="/" size="small">
          ← Back to search
        </Button>
      </Box>

      <PersonDetailTabs person={person} connections={connections} />
    </Stack>
  );
}
