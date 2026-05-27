"use client";

import {
  useCallback,
  useEffect,
  useState,
  type ReactElement,
  type SyntheticEvent,
} from "react";

import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Tab from "@mui/material/Tab";
import Tabs from "@mui/material/Tabs";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import type { ApiResponse, Person, PublicLink } from "@/lib/api-types";
import { bffFetch, bffFetchEnvelope } from "@/lib/api-client";
import type {
  PersonIdentifier,
  PersonMatchDecision,
  PersonSharedIdentifierCandidate,
} from "@/lib/api-types-person";
import { formatDateTime, formatDob } from "@/lib/display";
import { statusColor } from "@/lib/display";
import AuditTab from "./AuditTab";
import BankruptcyCasesCard from "./BankruptcyCasesCard";
import ConnectionsCard from "./ConnectionsCard";
import Gate from "./auth/Gate";
import IdentifiersSection from "./IdentifiersSection";
import ManualMergeDialog from "./ManualMergeDialog";
import MatchesTab from "./MatchesTab";
import PersonFocusedGraph from "./PersonFocusedGraph";
import PersonSection from "./PersonSection";
import PersonTimelineTab from "./PersonTimelineTab";
import PossibleMatchesSection from "./PossibleMatchesSection";
import SalesCard from "./SalesCard";
import SurvivorshipOverrideDialog from "./SurvivorshipOverrideDialog";

interface Props {
  person: Person;
}

interface TabCounts {
  possibleMatches: number | null;
  identifiers: number | null;
  matchDecisions: number | null;
}

const EMPTY_TAB_COUNTS: TabCounts = {
  possibleMatches: null,
  identifiers: null,
  matchDecisions: null,
};

function tabLabel(label: string, count: number | null): string {
  return count === null ? label : `${label} (${count})`;
}

export default function PersonDetailTabs({ person }: Props): ReactElement {
  const [tab, setTab] = useState<number>(() => {
    if (typeof window === "undefined") return 0;
    return new URLSearchParams(window.location.search).get("tab") === "timeline"
      ? 1
      : 0;
  });
  const [timelineTargetPk, setTimelineTargetPk] = useState<string | null>(
    () => {
      if (typeof window === "undefined") return null;
      return new URLSearchParams(window.location.search).get("sourceRecordPk");
    },
  );
  const [timelineTargetAt, setTimelineTargetAt] = useState<string | null>(
    () => {
      if (typeof window === "undefined") return null;
      return new URLSearchParams(window.location.search).get("timelineAt");
    },
  );
  const [mergeOpen, setMergeOpen] = useState<boolean>(false);
  const [overrideOpen, setOverrideOpen] = useState<boolean>(false);
  const [shareLink, setShareLink] = useState<PublicLink | null>(null);
  const [shareLoading, setShareLoading] = useState<boolean>(false);
  const [refreshedPerson, setRefreshedPerson] = useState<Person | null>(null);
  const [detailRefreshKey, setDetailRefreshKey] = useState<number>(0);
  const [tabCounts, setTabCounts] = useState<TabCounts>(EMPTY_TAB_COUNTS);
  const currentPerson = refreshedPerson?.person_id === person.person_id ? refreshedPerson : person;

  const refreshDetailData = useCallback((): void => {
    setDetailRefreshKey((current) => current + 1);
    void bffFetch<Person>(`/bff/persons/${encodeURIComponent(person.person_id)}`)
      .then(setRefreshedPerson)
      .catch(() => undefined);
  }, [person.person_id]);

  useEffect(() => {
    let cancelled = false;
    const personId = currentPerson.person_id;

    const loadCounts = async (): Promise<void> => {
      try {
        const [possibleMatches, identifiers, matchDecisions] = await Promise.all([
          bffFetchEnvelope<PersonSharedIdentifierCandidate[]>(
            `/bff/persons/${encodeURIComponent(personId)}/shared-identifiers?limit=1`,
          ),
          bffFetchEnvelope<PersonIdentifier[]>(
            `/bff/persons/${encodeURIComponent(personId)}/identifiers?limit=1`,
          ),
          bffFetchEnvelope<PersonMatchDecision[]>(
            `/bff/persons/${encodeURIComponent(personId)}/matches?limit=1`,
          ),
        ]);
        if (cancelled) return;
        setTabCounts({
          possibleMatches:
            possibleMatches.meta.total_count ?? possibleMatches.data.length,
          identifiers: identifiers.meta.total_count ?? identifiers.data.length,
          matchDecisions: matchDecisions.meta.total_count ?? matchDecisions.data.length,
        });
      } catch {
        if (!cancelled) setTabCounts(EMPTY_TAB_COUNTS);
      }
    };

    void loadCounts();
    return () => {
      cancelled = true;
    };
  }, [currentPerson.person_id, detailRefreshKey]);
  useEffect(() => {
    const listener = (event: Event): void => {
      const custom = event as CustomEvent<{ timestamp?: string }>;
      if (typeof custom.detail.timestamp === "string") {
        setTimelineTargetAt(custom.detail.timestamp);
        setTimelineTargetPk(null);
        setTab(1);
      }
    };
    window.addEventListener("timeline-target-change", listener);
    return () => window.removeEventListener("timeline-target-change", listener);
  }, []);

  const handleChange = (_e: SyntheticEvent, value: number): void => {
    setTab(value);
    const params = new URLSearchParams(window.location.search);
    if (value === 1) params.set("tab", "timeline");
    else params.delete("tab");
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}?${params.toString()}`,
    );
  };

  async function handleShare(): Promise<void> {
    setShareLoading(true);
    try {
      const res = await fetch(
        `/bff/persons/${encodeURIComponent(currentPerson.person_id)}/public-link`,
        { method: "POST" },
      );
      if (!res.ok) return;
      const json = (await res.json()) as ApiResponse<PublicLink>;
      setShareLink(json.data);
    } finally {
      setShareLoading(false);
    }
  }

  return (
    <Box>
      <Tabs value={tab} onChange={handleChange} sx={{ mb: 2 }}>
        <Tab label="Profile" />
        <Tab label="Timeline" />
        <Tab label={tabLabel("Possible Matches", tabCounts.possibleMatches)} />
        <Tab label={tabLabel("Identifiers", tabCounts.identifiers)} />
        <Tab label={tabLabel("Match Decisions", tabCounts.matchDecisions)} />
        <Tab label="Audit" />
      </Tabs>

      {tab === 0 ? (
        <Stack spacing={2}>
          <PersonHeader
            person={currentPerson}
            onMergeClick={() => setMergeOpen(true)}
            onOverrideClick={() => setOverrideOpen(true)}
            onShareClick={() => void handleShare()}
            shareLoading={shareLoading}
          />
          <ConnectionsCard personId={currentPerson.person_id} />
          <SalesCard personId={currentPerson.person_id} />
          <BankruptcyCasesCard personId={currentPerson.person_id} />
          <PersonGraphCard person={currentPerson} />
        </Stack>
      ) : null}
      {tab === 1 ? (
        <PersonTimelineTab
          personId={currentPerson.person_id}
          targetSourceRecordPk={timelineTargetPk}
          targetTimestamp={timelineTargetAt}
          onTargetConsumed={() => {
            setTimelineTargetPk(null);
            setTimelineTargetAt(null);
          }}
        />
      ) : null}
      {tab === 2 ? (
        <PersonSection title="Possible Matches">
          <PossibleMatchesSection
            personId={currentPerson.person_id}
            personName={currentPerson.preferred_full_name}
            refreshKey={detailRefreshKey}
            onMerged={refreshDetailData}
          />
        </PersonSection>
      ) : null}
      {tab === 3 ? (
        <PersonSection title="Identifiers">
          <IdentifiersSection personId={currentPerson.person_id} />
        </PersonSection>
      ) : null}
      {tab === 4 ? <MatchesTab personId={currentPerson.person_id} /> : null}
      {tab === 5 ? (
        <PersonSection title="Audit">
          <AuditTab
            personId={currentPerson.person_id}
            refreshKey={detailRefreshKey}
            onUnmerged={refreshDetailData}
          />
        </PersonSection>
      ) : null}

      <ManualMergeDialog
        open={mergeOpen}
        fromPersonId={currentPerson.person_id}
        onClose={() => setMergeOpen(false)}
        onMerged={refreshDetailData}
      />
      <SurvivorshipOverrideDialog
        open={overrideOpen}
        personId={currentPerson.person_id}
        onClose={() => setOverrideOpen(false)}
      />
      <ShareLinkDialog link={shareLink} onClose={() => setShareLink(null)} />
    </Box>
  );
}

interface HeaderProps {
  person: Person;
  onMergeClick: () => void;
  onOverrideClick: () => void;
  onShareClick: () => void;
  shareLoading: boolean;
}

function PersonHeader({
  person,
  onMergeClick,
  onOverrideClick,
  onShareClick,
  shareLoading,
}: HeaderProps): ReactElement {
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
            {person.preferred_full_name ?? "Unnamed person"}
          </Typography>
          <Chip
            label={person.status}
            size="small"
            color={statusColor(person.status)}
          />
          {person.is_high_value ? (
            <Chip label="high value" size="small" color="primary" />
          ) : null}
          {person.is_high_risk ? (
            <Chip label="high risk" size="small" color="error" />
          ) : null}
        </Stack>
        <Stack direction="row" spacing={1}>
          <Button
            size="small"
            variant="text"
            onClick={onShareClick}
            disabled={shareLoading}
          >
            {shareLoading ? "Generating…" : "Share"}
          </Button>
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

      <Tooltip title={aliasText(person)}>
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
        >
          {aliasText(person)}
        </Typography>
      </Tooltip>

      <Divider sx={{ my: 2 }} />

      <Grid container spacing={2}>
        <Field label="NRIC" value={person.preferred_nric} mono />
        <Field label="Date of Birth" value={formatDob(person.preferred_dob)} />
        <Field label="Phone" value={person.preferred_phone} />
        <Field label="Email" value={person.preferred_email} />
        <Field
          label="Address"
          value={person.preferred_address?.normalized_full ?? null}
        />
        <Field
          label="Profile Completeness"
          value={`${(person.profile_completeness_score * 100).toFixed(0)}%`}
        />
        <Field label="Updated" value={formatDateTime(person.updated_at)} />
      </Grid>
    </Paper>
  );
}

function aliasText(person: Person): string {
  const aliases = [person.preferred_full_name].filter(
    (value): value is string => value !== null && value.trim().length > 0,
  );
  return aliases.length > 0 ? `Aliases: ${aliases.join(", ")}` : "No aliases available";
}


function PersonGraphCard({ person }: { person: Person }): ReactElement {
  const title = person.preferred_full_name ?? "Person";
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Typography variant="subtitle1" sx={{ mb: 1 }}>
        Graph
      </Typography>
      <Box sx={{ height: 520 }}>
        <PersonFocusedGraph
          initialPersonId={person.person_id}
          initialTitle={title}
        />
      </Box>
    </Paper>
  );
}

interface ShareLinkDialogProps {
  link: PublicLink | null;
  onClose: () => void;
}

function ShareLinkDialog({
  link,
  onClose,
}: ShareLinkDialogProps): ReactElement {
  const [copied, setCopied] = useState<boolean>(false);
  const url =
    link !== null && typeof window !== "undefined"
      ? `${window.location.origin}/public/persons/${link.token}`
      : "";

  function handleCopy(): void {
    void navigator.clipboard.writeText(url).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  return (
    <Dialog open={link !== null} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Share profile link</DialogTitle>
      <DialogContent>
        <Stack spacing={2}>
          <TextField
            value={url}
            fullWidth
            size="small"
            slotProps={{
              input: {
                readOnly: true,
                endAdornment: (
                  <InputAdornment position="end">
                    <Tooltip title={copied ? "Copied!" : "Copy"}>
                      <IconButton size="small" onClick={handleCopy} edge="end">
                        <Typography variant="caption">
                          {copied ? "✓" : "Copy"}
                        </Typography>
                      </IconButton>
                    </Tooltip>
                  </InputAdornment>
                ),
              },
            }}
          />
          {link !== null ? (
            <Typography variant="caption" color="text.secondary">
              Expires: {formatDateTime(link.expires_at)}
            </Typography>
          ) : null}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}

interface FieldProps {
  label: string;
  value: string | null;
  cols?: 1 | 2 | 3;
  mono?: boolean;
}

function Field({
  label,
  value,
  cols = 1,
  mono = false,
}: FieldProps): ReactElement {
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
