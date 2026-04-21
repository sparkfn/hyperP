import type { ReactElement } from "react";

import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Grid2 from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import StartIngestionRunDialog from "@/components/StartIngestionRunDialog";
import Gate from "@/components/auth/Gate";
import { apiFetch } from "@/lib/api-server";
import type { SourceSystemInfo } from "@/lib/api-types-ops";

export default async function IngestionPage(): Promise<ReactElement> {
  const res = await apiFetch<SourceSystemInfo[]>("/source-systems");
  const systems: SourceSystemInfo[] = res.data;

  return (
    <Stack spacing={3}>
      <Box>
        <Typography variant="h4" fontWeight={600}>
          Ingestion
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Start ingestion runs for connected source systems.
        </Typography>
      </Box>

      {systems.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No source systems are registered.
        </Typography>
      ) : (
        <Grid2 container spacing={2}>
          {systems.map((s) => (
            <Grid2 key={s.source_key} size={{ xs: 12, md: 6 }}>
              <Paper variant="outlined" sx={{ p: 2 }}>
                <Stack spacing={1}>
                  <Stack direction="row" justifyContent="space-between" alignItems="center">
                    <Typography variant="h6">{s.display_name ?? s.source_key}</Typography>
                    <Chip
                      size="small"
                      label={s.is_active ? "active" : "inactive"}
                      color={s.is_active ? "success" : "default"}
                    />
                  </Stack>
                  <Typography variant="caption" color="text.secondary">
                    source_key: {s.source_key}
                  </Typography>
                  {s.system_type !== null ? (
                    <Typography variant="caption" color="text.secondary">
                      type: {s.system_type}
                    </Typography>
                  ) : null}
                  <Box>
                    <Gate mode="mutator" entityKey={s.entity_key} disableInsteadOfHide>
                      <StartIngestionRunDialog sourceKey={s.source_key} />
                    </Gate>
                  </Box>
                </Stack>
              </Paper>
            </Grid2>
          ))}
        </Grid2>
      )}
    </Stack>
  );
}
