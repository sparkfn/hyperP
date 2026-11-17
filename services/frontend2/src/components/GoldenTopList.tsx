import type { ReactElement } from "react";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import type { GoldenTopValue } from "@/lib/api-types";

interface GoldenTopListProps {
  label: string;
  entries: GoldenTopValue[];
}

function evidenceLine(entry: GoldenTopValue): string {
  const parts = [`score ${entry.score}`, `${entry.source_count} sources`];
  if (entry.last_confirmed_at !== null) {
    parts.push(entry.last_confirmed_at);
  }
  return parts.join(" · ");
}

export function GoldenTopList({ label, entries }: GoldenTopListProps): ReactElement {
  if (entries.length === 0) {
    return <></>;
  }
  return (
    <Stack spacing={0.5} sx={{ mt: 1 }}>
      <Typography variant="caption" color="text.secondary">
        {label} runners-up
      </Typography>
      {entries.map((entry) => (
        <Stack
          key={`${entry.rank}-${entry.value}`}
          direction="row"
          spacing={1}
          alignItems="center"
        >
          <Typography variant="body2">
            #{entry.rank} {entry.value}
          </Typography>
          {entry.verified ? <Chip label="Verified" size="small" color="success" /> : null}
          <Typography variant="caption" color="text.secondary">
            {evidenceLine(entry)}
          </Typography>
        </Stack>
      ))}
    </Stack>
  );
}
