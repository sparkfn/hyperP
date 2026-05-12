"use client";

import { useState, type ReactElement } from "react";

import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import Grid from "@mui/material/Grid2";
import Paper from "@mui/material/Paper";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import PaginationBar from "@/components/PaginationBar";
import type {
  PersonSourceRecord,
  SourceRecordAddressPayload,
  SourceRecordAttributePayload,
  SourceRecordChatMemberPayload,
  SourceRecordIdentifierPayload,
  SourceRecordInquiryPayload,
} from "@/lib/api-types-person";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";

interface Props {
  personId: string;
  onViewInTimeline: (sourceRecordPk: string) => void;
}

export default function SourceRecordsTab({
  personId,
  onViewInTimeline,
}: Props): ReactElement {
  const [selectedRecord, setSelectedRecord] =
    useState<PersonSourceRecord | null>(null);
  const {
    rows,
    error,
    loading,
    from,
    to,
    total,
    hasPrev,
    hasNext,
    goNext,
    goPrev,
  } = usePaginatedFetch<PersonSourceRecord>(
    `/bff/persons/${encodeURIComponent(personId)}/source-records`,
  );

  if (error !== null) return <Alert severity="error">{error}</Alert>;
  if (rows === null) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
        <CircularProgress size={24} />
      </Box>
    );
  }
  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No source records linked to this person.
      </Typography>
    );
  }

  return (
    <>
      <Paper elevation={0} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Source system</TableCell>
              <TableCell>Entity</TableCell>
              <TableCell>Source record id</TableCell>
              <TableCell>Type</TableCell>
              <TableCell>Link status</TableCell>
              <TableCell>Observed</TableCell>
              <TableCell>Ingested</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((record) => {
              const isConversation: boolean =
                record.record_type === "conversation";
              return (
                <TableRow
                  key={record.source_record_pk}
                  hover
                  tabIndex={0}
                  onClick={() => setSelectedRecord(record)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      setSelectedRecord(record);
                    }
                  }}
                  sx={{ cursor: "pointer" }}
                >
                  <TableCell>{record.source_system}</TableCell>
                  <TableCell>{entityLabel(record)}</TableCell>
                  <TableCell>{record.source_record_id}</TableCell>
                  <TableCell>
                    <Chip
                      label={record.record_type}
                      size="small"
                      color={isConversation ? "warning" : "default"}
                    />
                  </TableCell>
                  <TableCell>{record.link_status}</TableCell>
                  <TableCell>{record.observed_at}</TableCell>
                  <TableCell>{record.ingested_at}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Paper>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
        Click a record to view source record details.
      </Typography>
      <PaginationBar
        from={from}
        to={to}
        total={total}
        hasPrev={hasPrev}
        hasNext={hasNext}
        loading={loading}
        onPrev={goPrev}
        onNext={goNext}
      />
      <RecordPayloadDialog
        record={selectedRecord}
        onClose={() => setSelectedRecord(null)}
        onViewInTimeline={onViewInTimeline}
      />
    </>
  );
}

function RecordPayloadDialog({
  record,
  onClose,
  onViewInTimeline,
}: {
  record: PersonSourceRecord | null;
  onClose: () => void;
  onViewInTimeline: (sourceRecordPk: string) => void;
}): ReactElement {
  const payload = record?.normalized_payload ?? null;
  const identifiers = payload?.identifiers ?? [];
  const attributes = payload?.attributes ?? [];
  const address = payload?.address ?? null;
  const summary = firstText(payload?.summary, record?.raw_payload?.summary);

  return (
    <Dialog open={record !== null} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle>Source record details</DialogTitle>
      <DialogContent dividers>
        {record === null ? null : (
          <Stack spacing={3}>
            <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
              <Chip label={record.source_system} size="small" />
              <Chip label={entityLabel(record)} size="small" variant="outlined" />
              <Chip label={record.record_type} size="small" color={record.record_type === "conversation" ? "warning" : "default"} />
              <Chip label={record.link_status} size="small" variant="outlined" />
            </Stack>
            <Grid container spacing={2}>
              <PayloadMeta label="Source record id" value={record.source_record_id} />
              <PayloadMeta label="Entity" value={entityLabel(record)} />
              <PayloadMeta label="Extraction method" value={record.extraction_method ?? "—"} />
              <PayloadMeta label="Observed" value={record.observed_at} />
              <PayloadMeta label="Ingested" value={record.ingested_at} />
              <PayloadMeta
                label="Extraction confidence"
                value={record.extraction_confidence?.toFixed(2) ?? "—"}
              />
            </Grid>
            <Divider />
            {payload === null ? (
              <Typography variant="body2" color="text.secondary">
                No normalized payload was stored for this source record.
              </Typography>
            ) : (
              <Stack spacing={2.5}>
                {summary !== null ? (
                  <PayloadSection title="Summary" body={summary} />
                ) : null}
                <IdentifierSection identifiers={identifiers} />
                <AddressSection address={address} />
                <AttributeSection attributes={attributes} />
              </Stack>
            )}
            {record.record_type === "conversation" ? <ConversationPayload record={record} /> : null}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        {record !== null ? (
          <Button
            onClick={() => {
              onViewInTimeline(record.source_record_pk);
              onClose();
            }}
          >
            View in timeline
          </Button>
        ) : null}
        <Button onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}

function PayloadMeta({
  label,
  value,
}: {
  label: string;
  value: string;
}): ReactElement {
  return (
    <Grid size={{ xs: 12, sm: 6, md: 3 }}>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="body2" sx={{ wordBreak: "break-word" }}>
        {value || "—"}
      </Typography>
    </Grid>
  );
}

function PayloadSection({
  title,
  body,
}: {
  title: string;
  body: string;
}): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        {title}
      </Typography>
      <Paper variant="outlined" sx={{ p: 1.5, bgcolor: "grey.50" }}>
        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
          {body}
        </Typography>
      </Paper>
    </Box>
  );
}

function ConversationPayload({ record }: { record: PersonSourceRecord }): ReactElement | null {
  const raw = record.raw_payload;
  const normalized = record.normalized_payload;
  const transcript = firstText(raw?.conversation_text, raw?.messages_text);
  const summary = firstText(normalized?.summary, raw?.summary);
  const sentiment = firstText(normalized?.customer_sentiment, raw?.customer_sentiment);
  const chatMembers = firstList(normalized?.chat_members, raw?.chat_members);
  const inquiries = firstList(normalized?.inquiries, raw?.inquiries);
  const refEntries = conversationRefEntries(record);

  if (
    transcript === null &&
    summary === null &&
    sentiment === null &&
    chatMembers.length === 0 &&
    inquiries.length === 0 &&
    refEntries.length === 0
  ) {
    return null;
  }

  return (
    <Stack spacing={2.5}>
      {summary !== null ? <PayloadSection title="Conversation summary" body={summary} /> : null}
      {sentiment !== null ? <PayloadSection title="Customer sentiment" body={sentiment} /> : null}
      {chatMembers.length > 0 ? <ChatMembersSection members={chatMembers} /> : null}
      {inquiries.length > 0 ? <InquiriesSection inquiries={inquiries} /> : null}
      {refEntries.length > 0 ? <ConversationRefSection entries={refEntries} /> : null}
      {transcript !== null ? <PayloadSection title="Conversation" body={transcript} /> : null}
    </Stack>
  );
}

function ChatMembersSection({ members }: { members: SourceRecordChatMemberPayload[] }): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        Chat members
      </Typography>
      <Stack spacing={1}>
        {members.map((member, index) => (
          <Paper key={`${member.name ?? member.phone ?? "member"}-${index}`} variant="outlined" sx={{ p: 1.5 }}>
            <Grid container spacing={1.5}>
              <PayloadMeta label="Name" value={member.name ?? "—"} />
              <PayloadMeta label="Phone" value={member.phone ?? "—"} />
              <PayloadMeta label="Role" value={member.role ?? "—"} />
              <PayloadMeta label="Notes" value={member.notes ?? "—"} />
            </Grid>
          </Paper>
        ))}
      </Stack>
    </Box>
  );
}

function InquiriesSection({ inquiries }: { inquiries: SourceRecordInquiryPayload[] }): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        Inquiries
      </Typography>
      <Stack spacing={1}>
        {inquiries.map((inquiry, index) => (
          <Paper key={`${inquiry.machine_product ?? inquiry.unit ?? "inquiry"}-${index}`} variant="outlined" sx={{ p: 1.5 }}>
            <Grid container spacing={1.5}>
              <PayloadMeta label="Machine/product" value={inquiry.machine_product ?? "—"} />
              <PayloadMeta label="Unit" value={inquiry.unit ?? "—"} />
              <PayloadMeta label="LTA tag" value={inquiry.lta_tag ?? "—"} />
              <PayloadMeta label="Serial number" value={inquiry.serial_number ?? "—"} />
              <PayloadMeta label="Notes" value={inquiry.notes ?? "—"} />
            </Grid>
          </Paper>
        ))}
      </Stack>
    </Box>
  );
}

function ConversationRefSection({ entries }: { entries: Array<[string, string]> }): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        Conversation reference
      </Typography>
      <Grid container spacing={2}>
        {entries.map(([label, value]) => (
          <PayloadMeta key={label} label={labelize(label)} value={value} />
        ))}
      </Grid>
    </Box>
  );
}

function IdentifierSection({
  identifiers,
}: {
  identifiers: SourceRecordIdentifierPayload[];
}): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        Identifiers
      </Typography>
      {identifiers.length === 0 ? (
        <EmptyPayloadText>No normalized identifiers.</EmptyPayloadText>
      ) : (
        <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">
          {identifiers.map((identifier, index) => (
            <Chip
              key={`${identifier.identifier_type ?? "identifier"}-${identifier.normalized_value ?? index}`}
              label={`${labelize(identifier.identifier_type)}: ${identifier.normalized_value ?? "—"}`}
              size="small"
              color={identifier.is_verified === true ? "success" : "default"}
              variant={identifier.is_verified === true ? "filled" : "outlined"}
            />
          ))}
        </Stack>
      )}
    </Box>
  );
}

function AddressSection({
  address,
}: {
  address: SourceRecordAddressPayload | null;
}): ReactElement {
  if (address === null) {
    return (
      <Box>
        <Typography variant="subtitle2" gutterBottom>
          Address
        </Typography>
        <EmptyPayloadText>No normalized address.</EmptyPayloadText>
      </Box>
    );
  }

  const fields = [
    ["Full address", address.normalized_full],
    ["Unit", address.unit_number],
    ["Street number", address.street_number],
    ["Street", address.street_name],
    ["City", address.city],
    ["Postal code", address.postal_code],
    ["Country", address.country_code],
    ["Quality", address.quality_flag],
  ] as const;

  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        Address
      </Typography>
      <Grid container spacing={1.5}>
        {fields.map(([label, value]) => (
          <PayloadMeta key={label} label={label} value={value ?? "—"} />
        ))}
      </Grid>
    </Box>
  );
}

function AttributeSection({
  attributes,
}: {
  attributes: SourceRecordAttributePayload[];
}): ReactElement {
  return (
    <Box>
      <Typography variant="subtitle2" gutterBottom>
        Attributes
      </Typography>
      {attributes.length === 0 ? (
        <EmptyPayloadText>No normalized attributes.</EmptyPayloadText>
      ) : (
        <Grid container spacing={1.5}>
          {attributes.map((attribute, index) => (
            <PayloadMeta
              key={`${attribute.attribute_name ?? "attribute"}-${index}`}
              label={labelize(attribute.attribute_name)}
              value={attribute.attribute_value ?? "—"}
            />
          ))}
        </Grid>
      )}
    </Box>
  );
}

function EmptyPayloadText({ children }: { children: string }): ReactElement {
  return (
    <Typography variant="body2" color="text.secondary">
      {children}
    </Typography>
  );
}

function firstText(...values: Array<string | null | undefined>): string | null {
  for (const value of values) {
    if (value !== null && value !== undefined && value.trim().length > 0) {
      return value;
    }
  }
  return null;
}

function firstList<T>(...values: Array<T[] | null | undefined>): T[] {
  for (const value of values) {
    if (value !== null && value !== undefined && value.length > 0) {
      return value;
    }
  }
  return [];
}

function entityLabel(record: PersonSourceRecord): string {
  return record.entity_display_name ?? record.entity_key ?? "—";
}

function conversationRefEntries(record: PersonSourceRecord): Array<[string, string]> {
  const ref = record.conversation_ref;
  if (ref === null) return [];
  const keys = ["platform", "tenant", "chat_id", "deal_id", "bitrix_chat_id", "whatsapp_user_id", "session_id"] as const;
  return keys.flatMap((key): Array<[string, string]> => {
    const value = ref[key];
    return value !== undefined && value.length > 0 ? [[key, value]] : [];
  });
}

function labelize(value: string | undefined): string {
  if (value === undefined || value.length === 0) return "Unknown";
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
