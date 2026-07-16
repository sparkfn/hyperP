import { bffFetch } from "@/lib/api-client";
import { formatDob } from "@/lib/display";
import { sourceRecordReference } from "@/lib/ui-display";
import type { Person } from "@/lib/api-types";
import type {
  GoldenProfileSelectionRequestBody,
  PersonIdentifier,
  PersonSourceRecord,
} from "@/lib/api-types-person";

export type GoldenProfileFieldName = GoldenProfileSelectionRequestBody["field_name"];
export type GoldenProfileSourceKind = "source_record_fact" | "identifier" | "address";

export interface GoldenProfileChoice {
  key: string;
  fieldName: GoldenProfileFieldName;
  label: string;
  value: string;
  displayValue: string;
  sourceKind: GoldenProfileSourceKind;
  sourceRecordPk: string | null;
  identifierType: string | null;
  sourceLabel: string;
  personId: string;
  isSurvivorDefault: boolean;
  observedAt: string;
}

export interface GoldenProfileEvidence {
  person: Person;
  sourceRecords: PersonSourceRecord[];
  identifiers: PersonIdentifier[];
}

const FIELD_LABELS: Record<GoldenProfileFieldName, string> = {
  preferred_full_name: "Full name",
  preferred_dob: "Date of birth",
  preferred_phone: "Phone",
  preferred_email: "Email",
  preferred_address: "Address",
  preferred_nric: "NRIC",
};

const FACT_FIELDS: ReadonlyArray<{ fieldName: GoldenProfileFieldName; attributeName: string }> = [
  { fieldName: "preferred_full_name", attributeName: "full_name" },
  { fieldName: "preferred_dob", attributeName: "dob" },
  { fieldName: "preferred_phone", attributeName: "phone" },
  { fieldName: "preferred_email", attributeName: "email" },
];

const IDENTIFIER_FIELDS: Readonly<Record<string, GoldenProfileFieldName>> = {
  phone: "preferred_phone",
  mobile: "preferred_phone",
  email: "preferred_email",
  nric: "preferred_nric",
};

export function goldenProfileFieldLabel(fieldName: GoldenProfileFieldName): string {
  return FIELD_LABELS[fieldName];
}

export function buildGoldenProfileChoices(
  survivorPersonId: string,
  evidences: readonly GoldenProfileEvidence[],
): GoldenProfileChoice[] {
  const choices: GoldenProfileChoice[] = [];
  for (const evidence of evidences) {
    choices.push(...personGoldenChoices(survivorPersonId, evidence));
  }
  return uniqueChoices(choices);
}

export function appendPageLimit(path: string): string {
  return `${path}${path.includes("?") ? "&" : "?"}limit=100`;
}

export async function loadGoldenProfileEvidence(personId: string): Promise<GoldenProfileEvidence> {
  const [person, sourceRecords, identifiers] = await Promise.all([
    bffFetch<Person>(`/bff/persons/${encodeURIComponent(personId)}`),
    bffFetch<PersonSourceRecord[]>(appendPageLimit(`/bff/persons/${encodeURIComponent(personId)}/source-records`)),
    bffFetch<PersonIdentifier[]>(appendPageLimit(`/bff/persons/${encodeURIComponent(personId)}/identifiers`)),
  ]);
  return { person, sourceRecords, identifiers };
}

export function selectionBody(choice: GoldenProfileChoice): GoldenProfileSelectionRequestBody {
  return {
    field_name: choice.fieldName,
    source_kind: choice.sourceKind,
    selected_value: choice.value,
    source_record_pk: choice.sourceRecordPk,
    identifier_type: choice.identifierType,
  };
}

export function defaultChoiceByField(
  choices: readonly GoldenProfileChoice[],
): Partial<Record<GoldenProfileFieldName, string>> {
  const defaults: Partial<Record<GoldenProfileFieldName, string>> = {};
  for (const choice of choices) {
    if (choice.isSurvivorDefault && defaults[choice.fieldName] === undefined) {
      defaults[choice.fieldName] = choice.key;
    }
  }
  return defaults;
}

function personGoldenChoices(
  survivorPersonId: string,
  evidence: GoldenProfileEvidence,
): GoldenProfileChoice[] {
  return [
    ...sourceRecordFactChoices(survivorPersonId, evidence),
    ...sourceRecordAddressChoices(survivorPersonId, evidence),
    ...identifierChoices(survivorPersonId, evidence),
  ];
}

function sourceRecordFactChoices(
  survivorPersonId: string,
  evidence: GoldenProfileEvidence,
): GoldenProfileChoice[] {
  return evidence.sourceRecords.flatMap((record) =>
    (record.normalized_payload?.attributes ?? []).flatMap((attribute) => {
      const mapping = FACT_FIELDS.find((field) => field.attributeName === attribute.attribute_name);
      const value = attribute.attribute_value;
      if (mapping === undefined || value === undefined || value.trim() === "") {
        return [];
      }
      return [
        makeChoice({
          survivorPersonId,
          personId: evidence.person.person_id,
          fieldName: mapping.fieldName,
          value,
          sourceKind: "source_record_fact",
          sourceRecordPk: record.source_record_pk,
          identifierType: null,
          sourceLabel: `${record.source_system} ${sourceRecordReference(record.source_record_id, record.source_system)}`,
          observedAt: record.observed_at,
        }),
      ];
    }),
  );
}

function sourceRecordAddressChoices(
  survivorPersonId: string,
  evidence: GoldenProfileEvidence,
): GoldenProfileChoice[] {
  return evidence.sourceRecords.flatMap((record) => {
    const value = record.normalized_payload?.address?.normalized_full;
    if (value === undefined || value === null || value.trim() === "") {
      return [];
    }
    return [
      makeChoice({
        survivorPersonId,
        personId: evidence.person.person_id,
        fieldName: "preferred_address",
        value,
        sourceKind: "address",
        sourceRecordPk: record.source_record_pk,
        identifierType: null,
        sourceLabel: `${record.source_system} ${sourceRecordReference(record.source_record_id, record.source_system)}`,
        observedAt: record.observed_at,
      }),
    ];
  });
}

function identifierChoices(
  survivorPersonId: string,
  evidence: GoldenProfileEvidence,
): GoldenProfileChoice[] {
  return evidence.identifiers.flatMap((identifier) => {
    const fieldName = IDENTIFIER_FIELDS[identifier.identifier_type.toLowerCase()];
    if (fieldName === undefined || identifier.normalized_value.trim() === "") {
      return [];
    }
    return [
      makeChoice({
        survivorPersonId,
        personId: evidence.person.person_id,
        fieldName,
        value: identifier.normalized_value,
        sourceKind: "identifier",
        sourceRecordPk: identifier.source_records[0]?.source_record_pk ?? null,
        identifierType: identifier.identifier_type,
        sourceLabel: `${identifier.identifier_type} identifier`,
        observedAt: identifier.last_confirmed_at ?? "",
      }),
    ];
  });
}

function makeChoice(args: {
  survivorPersonId: string;
  personId: string;
  fieldName: GoldenProfileFieldName;
  value: string;
  sourceKind: GoldenProfileSourceKind;
  sourceRecordPk: string | null;
  identifierType: string | null;
  sourceLabel: string;
  observedAt: string;
}): GoldenProfileChoice {
  const displayValue = args.fieldName === "preferred_dob" ? formatDob(args.value.slice(0, 10)) : args.value;
  return {
    key: [args.personId, args.fieldName, args.sourceKind, args.sourceRecordPk ?? "", args.value].join("|"),
    fieldName: args.fieldName,
    label: FIELD_LABELS[args.fieldName],
    value: args.value,
    displayValue,
    sourceKind: args.sourceKind,
    sourceRecordPk: args.sourceRecordPk,
    identifierType: args.identifierType,
    sourceLabel: args.sourceLabel,
    personId: args.personId,
    isSurvivorDefault: args.personId === args.survivorPersonId,
    observedAt: args.observedAt,
  };
}

function uniqueChoices(choices: readonly GoldenProfileChoice[]): GoldenProfileChoice[] {
  const seen = new Set<string>();
  const unique: GoldenProfileChoice[] = [];
  for (const choice of choices) {
    const key = `${choice.fieldName}|${choice.value}|${choice.sourceKind}|${choice.sourceRecordPk ?? ""}`;
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(choice);
    }
  }
  return unique;
}
