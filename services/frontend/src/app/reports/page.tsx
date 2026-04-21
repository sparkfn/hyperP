"use client";

import { useCallback, useEffect, useState, type ReactElement } from "react";
import { useRouter } from "next/navigation";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardActionArea from "@mui/material/CardActionArea";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import Gate from "@/components/auth/Gate";
import { BffError, bffFetch } from "@/lib/api-client";
import type { ReportSummary } from "@/lib/api-types";

export default function ReportsPage(): ReactElement {
  const router = useRouter();
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [seeding, setSeeding] = useState<boolean>(false);

  const loadReports = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await bffFetch<ReportSummary[]>("/api/reports");
      setReports(data);
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to load reports.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadReports();
  }, [loadReports]);

  async function handleSeed(): Promise<void> {
    setSeeding(true);
    try {
      await bffFetch<string[]>("/api/reports/seed", { method: "POST" });
      await loadReports();
    } catch (err: unknown) {
      const msg =
        err instanceof BffError || err instanceof Error
          ? err.message
          : "Failed to seed reports.";
      setError(msg);
    } finally {
      setSeeding(false);
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
        <CircularProgress />
      </Box>
    );
  }

  return (
    <Stack spacing={3}>
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Box>
          <Typography variant="h4" fontWeight={600}>
            Reports
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Custom Cypher reports for tabular data queries.
          </Typography>
        </Box>
        <Gate mode="admin">
          <Stack direction="row" spacing={1}>
            <Button
              variant="contained"
              size="small"
              onClick={() => router.push("/reports/new")}
            >
              Create Report
            </Button>
            <Button
              variant="outlined"
              size="small"
              disabled={seeding}
              onClick={() => void handleSeed()}
            >
              {seeding ? <CircularProgress size={18} /> : "Seed Samples"}
            </Button>
          </Stack>
        </Gate>
      </Box>

      {error !== null ? <Alert severity="error">{error}</Alert> : null}

      {reports.length === 0 ? (
        <Alert severity="info">
          No reports found. Click &quot;Seed Sample Reports&quot; to create sample report
          definitions.
        </Alert>
      ) : (
        <Box
          sx={{
            display: "grid",
            gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", md: "1fr 1fr 1fr" },
            gap: 2,
          }}
        >
          {reports.map((report) => (
            <Card key={report.report_key} variant="outlined">
              <CardActionArea onClick={() => router.push(`/reports/${report.report_key}`)}>
                <CardContent>
                  <Stack spacing={1}>
                    <Typography variant="h6" fontWeight={600}>
                      {report.display_name}
                    </Typography>
                    {report.description ? (
                      <Typography variant="body2" color="text.secondary">
                        {report.description}
                      </Typography>
                    ) : null}
                    {report.category ? (
                      <Box>
                        <Chip label={report.category} size="small" variant="outlined" />
                      </Box>
                    ) : null}
                  </Stack>
                </CardContent>
              </CardActionArea>
            </Card>
          ))}
        </Box>
      )}
    </Stack>
  );
}
