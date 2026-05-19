import type { Person } from "@/lib/api-types";
import type { PersonIdentifier, PersonSourceRecord } from "@/lib/api-types-person";

export type GoldenProfileFieldName =
  | "preferred_full_name"
  | "preferred_dob"
  | "preferred_phone"
  | "preferred_email"
  | "preferred_address"
  | "preferred_nric";

export type GoldenProfileSourceKind = "source_record_fact" | "identifier" | "address";

export interface GoldenProfileSelectionBody {
  field_name: GoldenProfileFieldName;
  source_kind: GoldenProfileSourceKind;
  selected_value: string;
  source_record_pk: string | null;
  identifier_type: string | null;
}

export interface GoldenProfileChoice {
  key: string;
  fieldName: GoldenProfileFieldName;
  label: string;
  value: string;
  sourceKind: GoldenProfileSourceKind;
  sourceRecordPk: string | null;
  identifierType: string | null;
  sourceLabel: string;
  personId: string;
  isSurvivorDefault: boolean;
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

const FACT_FIELDS: ReadonlyArray<{
  fieldName: GoldenProfileFieldName;
  attributeName: string;
}> = [
  { fieldName: "preferred_full_name", attributeName: "full_name" },
  { fieldName: "preferred_dob", attributeName: "dob" },
  { fieldName: "preferred_phone", attributeName: "phone" },
  { fieldName: "preferred_email", attributeName: "email" },
];

const IDENTIFIER_FIELDS: Record<string, GoldenProfileFieldName> = {
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

export function selectionBody(choice: GoldenProfileChoice): GoldenProfileSelectionBody {
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
  const choices: GoldenProfileChoice[] = [];
  choices.push(...currentGoldenChoices(survivorPersonId, evidence.person));
  choices.push(...sourceRecordFactChoices(survivorPersonId, evidence));
  choices.push(...sourceRecordAddressChoices(survivorPersonId, evidence));
  choices.push(...identifierChoices(survivorPersonId, evidence));
  return choices;
}

function currentGoldenChoices(_survivorPersonId: string, _person: Person): GoldenProfileChoice[] {
  return [];
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
          sourceLabel: `${record.source_system} ${record.source_record_id}`,
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
    if (value === undefined || value.trim() === "") {
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
        sourceLabel: `${record.source_system} ${record.source_record_id}`,
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
        sourceRecordPk: identifier.source_record_pks[0] ?? null,
        identifierType: identifier.identifier_type,
        sourceLabel: `${identifier.identifier_type} identifier`,
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
}): GoldenProfileChoice {
  return {
    key: [args.personId, args.fieldName, args.sourceKind, args.sourceRecordPk ?? "", args.value].join("|"),
    fieldName: args.fieldName,
    label: FIELD_LABELS[args.fieldName],
    value: args.value,
    sourceKind: args.sourceKind,
    sourceRecordPk: args.sourceRecordPk,
    identifierType: args.identifierType,
    sourceLabel: args.sourceLabel,
    personId: args.personId,
    isSurvivorDefault: args.personId === args.survivorPersonId,
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
