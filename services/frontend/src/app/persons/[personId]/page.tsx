import type { ReactElement } from "react";
import { notFound } from "next/navigation";

import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";

import BackButton from "@/components/BackButton";
import PersonDetailTabs from "@/components/PersonDetailTabs";
import { UpstreamError, apiFetch } from "@/lib/api-server";
import type { Person } from "@/lib/api-types";

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

export default async function PersonDetailPage({ params }: PageProps): Promise<ReactElement> {
  const { personId } = await params;
  const person = await loadPerson(personId);

  return (
    <Stack spacing={3}>
      <Box>
        <BackButton label="← Back to persons" />
      </Box>

      <PersonDetailTabs person={person} />
    </Stack>
  );
}
