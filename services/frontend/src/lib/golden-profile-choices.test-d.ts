import type { ApiResponse, ListedPerson, Person, PopoverDisplayItem } from "./api-types";
import type { PossibleMatchDetail } from "./api-types-person";
import {
  appendPageLimit,
  buildGoldenProfileChoices,
  type GoldenProfileEvidence,
} from "./golden-profile-choices";

const survivor: Person = {
  person_id: "person-b",
  status: "active",
  is_high_value: false,
  is_high_risk: false,
  preferred_full_name: "Jane Survivor",
  preferred_phone: null,
  preferred_email: "survivor@example.com",
  preferred_dob: null,
  preferred_address: null,
  preferred_nric: null,
  profile_completeness_score: 0,
  golden_profile_computed_at: null,
  golden_profile_version: null,
  source_record_count: 0,
  connection_count: 0,
  created_at: "2026-05-19T00:00:00Z",
  updated_at: "2026-05-19T00:00:00Z",
};

const displayResponse: ApiResponse<string[]> = {
  data: ["row"],
  meta: { request_id: "req-1", next_cursor: null },
  display_items: [{ primary: "Jane Survivor", secondary: "CRM" } satisfies PopoverDisplayItem],
};

const listedPerson: ListedPerson = {
  ...survivor,
  phone_confidence: null,
  entities: [],
  entity_count: 0,
  identifier_count: 1,
  possible_match_count: 2,
  order_count: 0,
  bankruptcy_case_count: 0,
};

const possibleMatchDetail: PossibleMatchDetail = {
  candidate_person_id: "person-c",
  candidate_name: "Candidate",
  shared_identifier_groups: [
    {
      identifier_type: "phone",
      normalized_value: "+6599990000",
      candidate_source_records: [],
      current_person_source_records: [],
    },
  ],
};

if (displayResponse.display_items?.[0]?.primary !== "Jane Survivor") {
  throw new Error("display_items were not exposed on ApiResponse");
}

if (listedPerson.possible_match_count !== 2) {
  throw new Error("possible_match_count was not exposed on ListedPerson");
}

if (possibleMatchDetail.shared_identifier_groups[0]?.candidate_source_records.length !== 0) {
  throw new Error("possible match detail source-record groups were not exposed");
}

const evidence: GoldenProfileEvidence = {
  person: survivor,
  sourceRecords: [],
  identifiers: [
    {
      identifier_type: "email",
      normalized_value: "identifier@example.com",
      is_active: true,
      is_verified: true,
      last_confirmed_at: null,
      source_system_key: "crm",
      source_record_pks: ["sr-1"],
      source_record_ids: ["record-1"],
      entities: [],
      source_records: [],
    },
    {
      identifier_type: "nric",
      normalized_value: "S1234567A",
      is_active: true,
      is_verified: true,
      last_confirmed_at: null,
      source_system_key: "crm",
      source_record_pks: ["sr-2"],
      source_record_ids: ["record-2"],
      entities: [],
      source_records: [],
    },
  ],
};

const choices = buildGoldenProfileChoices("person-b", [evidence]);
const emailChoice = choices.find(
  (choice) => choice.fieldName === "preferred_email" && choice.sourceKind === "identifier",
);
const nricChoice = choices.find((choice) => choice.fieldName === "preferred_nric");
const invalidEmailNricChoice = choices.find(
  (choice) => choice.fieldName === "preferred_nric" && choice.value === "identifier@example.com",
);

if (emailChoice?.identifierType !== "email") {
  throw new Error("email identifier choice was not mapped to preferred_email");
}

if (nricChoice?.identifierType !== "nric") {
  throw new Error("nric identifier choice was not mapped to preferred_nric");
}

if (invalidEmailNricChoice !== undefined) {
  throw new Error("email identifier was incorrectly offered as NRIC");
}

if (appendPageLimit("/bff/persons/person-1/source-records") !== "/bff/persons/person-1/source-records?limit=100") {
  throw new Error("page limit was not appended to URL without query string");
}

if (appendPageLimit("/bff/persons/person-1/identifiers?cursor=abc") !== "/bff/persons/person-1/identifiers?cursor=abc&limit=100") {
  throw new Error("page limit was not appended to URL with query string");
}
