"use client";

import { useEffect, useState, type ReactElement } from "react";
import Link from "next/link";
import type { UrlObject } from "url";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import { BffError, bffFetch } from "@/lib/api-client";
import type { EntitySummary } from "@/lib/api-types";

export default function EntitiesPage(): ReactElement {
  const [entities, setEntities] = useState<EntitySummary[] | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async (): Promise<void> => {
      try {
        const data = await bffFetch<EntitySummary[]>("/bff/entities");
        if (!cancelled) setEntities(data);
      } catch (err: unknown) {
        if (!cancelled) {
          const msg =
            err instanceof BffError || err instanceof Error
              ? err.message
              : "Failed to load entities.";
          setError(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return (): void => {
      cancelled = true;
    };
  }, []);

  return (
    <Stack spacing={1.5}>
      <Stack direction="row" alignItems="baseline" spacing={1}>
        <Typography variant="h5">Entities</Typography>
        <Typography variant="caption" color="text.secondary">
          Business units owning source systems.
        </Typography>
      </Stack>
      {loading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      ) : error ? (
        <Alert severity="error">{error}</Alert>
      ) : !entities || entities.length === 0 ? (
        <Alert severity="info">No entities found.</Alert>
      ) : (
        <Box
          sx={{
            display: "grid",
            gap: 1.5,
            gridTemplateColumns: {
              xs: "1fr",
              sm: "repeat(2, 1fr)",
              md: "repeat(3, 1fr)",
              lg: "repeat(4, 1fr)",
            },
          }}
        >
          {entities.map((e) => (
            <EntityCard key={e.entity_key} entity={e} />
          ))}
        </Box>
      )}
    </Stack>
  );
}

function EntityCard({ entity }: { entity: EntitySummary }): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 1.5, display: "flex", flexDirection: "column", gap: 1 }}>
      <Stack direction="row" alignItems="center" spacing={0.5} flexWrap="wrap" useFlexGap>
        <Typography variant="subtitle1" sx={{ flexGrow: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {entity.display_name ?? entity.entity_key}
        </Typography>
        {entity.is_active ? null : <Chip label="inactive" color="warning" />}
      </Stack>
      <Stack direction="row" spacing={0.5} flexWrap="wrap" useFlexGap>
        {entity.entity_type ? <Chip label={entity.entity_type} variant="outlined" /> : null}
        {entity.country_code ? <Chip label={entity.country_code} variant="outlined" /> : null}
      </Stack>
      <Divider />
      <Stack spacing={0.25}>
        <Metric
          label="Persons"
          value={String(entity.person_count)}
          href={{ pathname: "/persons", query: { entity_key: entity.entity_key } }}
        />
        <Metric label="Source records" value={String(entity.source_record_count)} />
        <Metric label="Active review cases" value={String(entity.active_review_cases)} />
        <Metric label="Last ingested" value={formatDate(entity.last_ingested_at)} />
      </Stack>
      <Box sx={{ flexGrow: 1 }} />
      <Link
        href={{ pathname: "/persons", query: { entity_key: entity.entity_key } }}
        style={{ textDecoration: "none" }}
      >
        <Button variant="outlined" fullWidth>
          View persons
        </Button>
      </Link>
    </Paper>
  );
}

interface MetricProps {
  label: string;
  value: string;
  href?: UrlObject;
}

function Metric({ label, value, href }: MetricProps): ReactElement {
  return (
    <Stack direction="row" justifyContent="space-between" alignItems="center">
      <Typography variant="caption" color="text.secondary">
        {label}
      </Typography>
      {href ? (
        <Link
          href={href}
          style={{ fontWeight: 600, fontSize: "0.85rem", textDecoration: "none", color: "#1f4e9e" }}
        >
          {value}
        </Link>
      ) : (
        <Typography variant="body2" sx={{ fontWeight: 600 }}>
          {value}
        </Typography>
      )}
    </Stack>
  );
}

function formatDate(value: string | null): string {
  if (!value) return "\u2014";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().slice(0, 10);
}
