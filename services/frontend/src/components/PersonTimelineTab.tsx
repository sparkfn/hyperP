"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
} from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";

import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ApiResponse } from "@/lib/api-types";
import { formatDateTime } from "@/lib/display";
import type {
  PersonTimelineFact,
  PersonTimelineGroup,
} from "@/lib/api-types-person";

const PAGE_SIZE = 10;

interface Props {
  personId: string;
  targetSourceRecordPk: string | null;
  targetTimestamp: string | null;
  onTargetConsumed: () => void;
}

export default function PersonTimelineTab({
  personId,
  targetSourceRecordPk,
  targetTimestamp,
  onTargetConsumed,
}: Props): ReactElement {
  const [groups, setGroups] = useState<PersonTimelineGroup[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingInitial, setLoadingInitial] = useState<boolean>(true);
  const [loadingOlder, setLoadingOlder] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [jumpDate, setJumpDate] = useState<string>("");
  const [jumpRecordPk, setJumpRecordPk] = useState<string>("");
  const [highlightedPk, setHighlightedPk] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const timelinePath = `/bff/persons/${encodeURIComponent(personId)}/timeline`;

  const loadPage = useCallback(
    async (
      cursor: string | null,
    ): Promise<ApiResponse<PersonTimelineGroup[]>> => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (cursor !== null) params.set("cursor", cursor);
      return bffFetchEnvelope<PersonTimelineGroup[]>(
        `${timelinePath}?${params.toString()}`,
      );
    },
    [timelinePath],
  );

  useEffect(() => {
    let cancelled = false;
    const run = async (): Promise<void> => {
      setLoadingInitial(true);
      setError(null);
      setGroups([]);
      try {
        const envelope = await loadPage(null);
        if (cancelled) return;
        setGroups(envelope.data);
        setNextCursor(envelope.meta.next_cursor);
        requestAnimationFrame(() => {
          const el = scrollRef.current;
          if (el !== null) el.scrollTop = el.scrollHeight;
        });
      } catch (err: unknown) {
        if (!cancelled)
          setError(
            err instanceof BffError ? err.message : "Failed to load timeline.",
          );
      } finally {
        if (!cancelled) setLoadingInitial(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [loadPage]);

  const loadOlder = useCallback(async (): Promise<void> => {
    if (nextCursor === null || loadingOlder) return;
    const el = scrollRef.current;
    const previousHeight = el?.scrollHeight ?? 0;
    setLoadingOlder(true);
    try {
      const envelope = await loadPage(nextCursor);
      setGroups((current) => [...current, ...envelope.data]);
      setNextCursor(envelope.meta.next_cursor);
      requestAnimationFrame(() => {
        const currentEl = scrollRef.current;
        if (currentEl !== null) {
          currentEl.scrollTop =
            currentEl.scrollHeight - previousHeight + currentEl.scrollTop;
        }
      });
    } catch (err: unknown) {
      setError(
        err instanceof BffError
          ? err.message
          : "Failed to load older timeline records.",
      );
    } finally {
      setLoadingOlder(false);
    }
  }, [loadPage, loadingOlder, nextCursor]);

  const handleScroll = useCallback((): void => {
    const el = scrollRef.current;
    if (el === null || el.scrollTop > 80) return;
    void loadOlder();
  }, [loadOlder]);

  const scrollToSourceRecord = useCallback(
    (sourceRecordPk: string): boolean => {
      const card = cardRefs.current.get(sourceRecordPk);
      if (card === undefined) return false;
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      setHighlightedPk(sourceRecordPk);
      window.setTimeout(() => setHighlightedPk(null), 1800);
      return true;
    },
    [],
  );

  const fetchTarget = useCallback(
    async (sourceRecordPk: string): Promise<void> => {
      const params = new URLSearchParams({ source_record_pk: sourceRecordPk });
      const envelope = await bffFetchEnvelope<PersonTimelineGroup>(
        `${timelinePath}/target?${params.toString()}`,
      );
      setGroups((current) => {
        if (
          current.some(
            (group) =>
              group.source_record_pk === envelope.data.source_record_pk,
          )
        ) {
          return current;
        }
        return [...current, envelope.data].sort((left, right) =>
          right.occurred_at.localeCompare(left.occurred_at),
        );
      });
      requestAnimationFrame(() =>
        scrollToSourceRecord(envelope.data.source_record_pk),
      );
    },
    [scrollToSourceRecord, timelinePath],
  );

  useEffect(() => {
    if (targetSourceRecordPk === null || loadingInitial) return;
    const run = async (): Promise<void> => {
      if (!scrollToSourceRecord(targetSourceRecordPk)) {
        try {
          await fetchTarget(targetSourceRecordPk);
        } catch (err: unknown) {
          setError(
            err instanceof BffError
              ? err.message
              : "Could not find that source record.",
          );
        }
      }
      onTargetConsumed();
    };
    void run();
  }, [
    fetchTarget,
    loadingInitial,
    onTargetConsumed,
    scrollToSourceRecord,
    targetSourceRecordPk,
  ]);

  useEffect(() => {
    if (targetTimestamp === null || loadingInitial) return;
    const targetTime = Date.parse(targetTimestamp);
    if (Number.isNaN(targetTime)) return;
    const closest = groups.reduce<PersonTimelineGroup | null>((best, group) => {
      const groupTime = Date.parse(group.occurred_at);
      if (Number.isNaN(groupTime)) return best;
      if (best === null) return group;
      return Math.abs(groupTime - targetTime) <
        Math.abs(Date.parse(best.occurred_at) - targetTime)
        ? group
        : best;
    }, null);
    if (closest !== null) scrollToSourceRecord(closest.source_record_pk);
    onTargetConsumed();
  }, [
    groups,
    loadingInitial,
    onTargetConsumed,
    scrollToSourceRecord,
    targetTimestamp,
  ]);

  const sourceRecordOptions = useMemo(
    () =>
      groups.map((group) => ({
        label: `${group.source_system} • ${group.source_record_id} • ${formatDateTime(group.occurred_at)}`,
        value: group.source_record_pk,
      })),
    [groups],
  );

  function handleJumpToDate(): void {
    if (jumpDate.trim() === "") return;
    const timestamp = new Date(jumpDate).toISOString();
    const params = new URLSearchParams(window.location.search);
    params.set("tab", "timeline");
    params.set("timelineAt", timestamp);
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}?${params.toString()}`,
    );
    window.dispatchEvent(
      new CustomEvent("timeline-target-change", { detail: { timestamp } }),
    );
  }

  function handleJumpToRecord(): void {
    if (jumpRecordPk === "") return;
    void fetchTarget(jumpRecordPk).catch((err: unknown) => {
      setError(
        err instanceof BffError
          ? err.message
          : "Could not find that source record.",
      );
    });
  }

  if (loadingInitial) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  return (
    <Stack spacing={2}>
      {error !== null ? <Alert severity="error">{error}</Alert> : null}
      <TimelineJumpControls
        jumpDate={jumpDate}
        jumpRecordPk={jumpRecordPk}
        sourceRecordOptions={sourceRecordOptions}
        onJumpDateChange={setJumpDate}
        onJumpRecordChange={setJumpRecordPk}
        onJumpToDate={handleJumpToDate}
        onJumpToRecord={handleJumpToRecord}
      />
      {groups.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No source-record facts are available for this person.
        </Typography>
      ) : (
        <Box
          ref={scrollRef}
          onScroll={handleScroll}
          sx={{
            border: 1,
            borderColor: "divider",
            borderRadius: 2,
            height: 640,
            overflowY: "auto",
            p: 2,
            bgcolor: "background.default",
          }}
        >
          {loadingOlder ? (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: "block", textAlign: "center", mb: 1 }}
            >
              Loading older records…
            </Typography>
          ) : null}
          <Stack spacing={1.5} sx={{ flexDirection: "column-reverse" }}>
            {groups.map((group) => (
              <TimelineCard
                key={group.source_record_pk}
                group={group}
                highlighted={highlightedPk === group.source_record_pk}
                setRef={(node) => {
                  if (node === null)
                    cardRefs.current.delete(group.source_record_pk);
                  else cardRefs.current.set(group.source_record_pk, node);
                }}
              />
            ))}
          </Stack>
        </Box>
      )}
    </Stack>
  );
}

interface TimelineJumpControlsProps {
  jumpDate: string;
  jumpRecordPk: string;
  sourceRecordOptions: { label: string; value: string }[];
  onJumpDateChange: (value: string) => void;
  onJumpRecordChange: (value: string) => void;
  onJumpToDate: () => void;
  onJumpToRecord: () => void;
}

function TimelineJumpControls({
  jumpDate,
  jumpRecordPk,
  sourceRecordOptions,
  onJumpDateChange,
  onJumpRecordChange,
  onJumpToDate,
  onJumpToRecord,
}: TimelineJumpControlsProps): ReactElement {
  return (
    <Paper variant="outlined" sx={{ p: 2 }}>
      <Stack
        direction={{ xs: "column", md: "row" }}
        spacing={1.5}
        alignItems={{ md: "center" }}
      >
        <TextField
          label="Jump to date/time"
          type="datetime-local"
          value={jumpDate}
          onChange={(event) => onJumpDateChange(event.target.value)}
          size="small"
          slotProps={{ inputLabel: { shrink: true } }}
        />
        <Button variant="outlined" onClick={onJumpToDate}>
          Jump
        </Button>
        <Divider flexItem orientation="vertical" />
        <TextField
          select
          label="Jump to loaded source record"
          value={jumpRecordPk}
          onChange={(event) => onJumpRecordChange(event.target.value)}
          size="small"
          sx={{ minWidth: 260 }}
        >
          {sourceRecordOptions.map((option) => (
            <MenuItem key={option.value} value={option.value}>
              {option.label}
            </MenuItem>
          ))}
        </TextField>
        <Button variant="outlined" onClick={onJumpToRecord}>
          Open card
        </Button>
      </Stack>
    </Paper>
  );
}

function TimelineCard({
  group,
  highlighted,
  setRef,
}: {
  group: PersonTimelineGroup;
  highlighted: boolean;
  setRef: (node: HTMLDivElement | null) => void;
}): ReactElement {
  return (
    <Box
      ref={setRef}
      sx={{ display: "grid", gridTemplateColumns: "32px 1fr", columnGap: 1.5 }}
    >
      <Box
        sx={{ position: "relative", display: "flex", justifyContent: "center" }}
      >
        <Box
          sx={{
            position: "absolute",
            top: 0,
            bottom: 0,
            width: 2,
            bgcolor: "divider",
          }}
        />
        <Box
          sx={{
            mt: 2,
            width: 14,
            height: 14,
            borderRadius: "50%",
            bgcolor: "primary.main",
            zIndex: 1,
          }}
        />
      </Box>
      <Paper
        variant="outlined"
        sx={{
          p: 1.5,
          transition: "background-color 180ms ease, border-color 180ms ease",
          bgcolor: highlighted ? "action.hover" : "background.paper",
          borderColor: highlighted ? "primary.main" : "divider",
        }}
      >
        <Stack spacing={1}>
          <Stack
            direction="row"
            spacing={1}
            useFlexGap
            flexWrap="wrap"
            alignItems="center"
          >
            <Chip label={group.source_system} size="small" />
            <Chip
              label={group.record_type}
              size="small"
              color={
                group.record_type === "conversation" ? "warning" : "default"
              }
            />
            <Chip
              label={
                group.timestamp_kind === "source"
                  ? "Source timestamp"
                  : "Fallback timestamp"
              }
              size="small"
              variant="outlined"
            />
          </Stack>
          <Box>
            <Typography variant="subtitle2">
              {group.source_record_id}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {formatDateTime(group.occurred_at)}
            </Typography>
          </Box>
          {group.facts.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No normalized facts stored for this source record.
            </Typography>
          ) : (
            <Stack spacing={0.75}>
              {group.facts.map((fact) => (
                <TimelineFactRow key={fact.fact_id} fact={fact} />
              ))}
            </Stack>
          )}
        </Stack>
      </Paper>
    </Box>
  );
}

function timelineFactColor(
  category: PersonTimelineFact["category"],
): "default" | "primary" | "secondary" | "error" | "info" | "success" | "warning" {
  return category === "bankruptcy" ? "warning" : "default";
}

function TimelineFactRow({ fact }: { fact: PersonTimelineFact }): ReactElement {
  return (
    <Stack direction="row" spacing={1} alignItems="baseline">
      <Chip
        label={fact.category}
        size="small"
        variant="outlined"
        color={timelineFactColor(fact.category)}
        sx={{ minWidth: 88 }}
      />
      <Box>
        <Typography variant="body2">
          <strong>{fact.label}:</strong> {fact.value}
        </Typography>
        {fact.detail !== null ? (
          <Typography variant="caption" color="text.secondary">
            {fact.detail}
          </Typography>
        ) : null}
      </Box>
    </Stack>
  );
}
