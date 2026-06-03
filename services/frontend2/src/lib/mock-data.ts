// Central mock data — realistic but not real.
// Used across all pages while UI is being built.

import type { ListedPerson, EntitySummary, SourceSystemSummary, ReportSummary, ReportDetail, PersonGraph, SalesOrder, PersonConnection } from "./api-types";
import type { ReviewCaseSummary, ReviewCaseDetail, SourceSystemInfo, IngestRunDetailResponse, DownstreamEvent, UserResponse, OAuthClient } from "./api-types-ops";
import type { PersonIdentifier, PersonSourceRecord, PersonAuditEvent, PersonBankruptcyCase } from "./api-types-person";

// ── Persons ───────────────────────────────────────────────────────────────────

export const MOCK_PERSONS: ListedPerson[] = [
  { person_id: "p-001", status: "active", is_high_value: true, is_high_risk: false, preferred_full_name: "Tan Wei Ming", preferred_phone: "+65 9123 4567", preferred_email: "weiming.tan@gmail.com", preferred_dob: "1985-03-12", preferred_nric: "S8512345A", preferred_address: { address_id: "a-1", unit_number: "#05-12", street_number: "18", street_name: "Orchard Road", city: "Singapore", postal_code: "238801", country_code: "SG", normalized_full: "18 Orchard Road #05-12, Singapore 238801" }, profile_completeness_score: 0.92, golden_profile_computed_at: "2026-05-06T08:00:00Z", golden_profile_version: "2", source_record_count: 4, connection_count: 3, created_at: "2024-01-10T00:00:00Z", updated_at: "2026-05-06T08:00:00Z", phone_confidence: 0.98, entities: [{ entity_key: "speedzone", display_name: "SpeedZone Retail", entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 2 }], entity_count: 1, identifier_count: 5, possible_match_count: 0, order_count: 12 },
  { person_id: "p-002", status: "active", is_high_value: true, is_high_risk: false, preferred_full_name: "Priya Ramasamy", preferred_phone: "+65 8234 5678", preferred_email: "priya.r@outlook.com", preferred_dob: "1990-07-22", preferred_nric: "S9023456B", preferred_address: { address_id: "a-2", unit_number: "#12-03", street_number: "10", street_name: "Tampines Avenue 5", city: "Singapore", postal_code: "529669", country_code: "SG", normalized_full: "10 Tampines Ave 5 #12-03, Singapore 529669" }, profile_completeness_score: 0.88, golden_profile_computed_at: "2026-05-05T14:00:00Z", golden_profile_version: "1", source_record_count: 3, connection_count: 1, created_at: "2024-02-15T00:00:00Z", updated_at: "2026-05-05T14:00:00Z", phone_confidence: 0.95, entities: [{ entity_key: "fundbox", display_name: "Fundbox Consumer", entity_type: "finance", country_code: "SG", is_active: true, source_record_count: 2 }], entity_count: 1, identifier_count: 4, possible_match_count: 0, order_count: 7 },
  { person_id: "p-003", status: "active", is_high_value: false, is_high_risk: true, preferred_full_name: "Mohamed Faizal Bin Hassan", preferred_phone: "+65 9345 6789", preferred_email: "faizal.h@yahoo.com", preferred_dob: "1978-11-05", preferred_nric: "S7834567C", preferred_address: null, profile_completeness_score: 0.61, golden_profile_computed_at: "2026-05-04T10:00:00Z", golden_profile_version: "1", source_record_count: 2, connection_count: 0, created_at: "2024-03-01T00:00:00Z", updated_at: "2026-05-04T10:00:00Z", phone_confidence: 0.72, entities: [{ entity_key: "speedzone", display_name: "SpeedZone Retail", entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 1 }], entity_count: 1, identifier_count: 2, possible_match_count: 0, order_count: 1 },
  { person_id: "p-004", status: "merged", is_high_value: false, is_high_risk: false, preferred_full_name: "Sarah Lim Hui Ling", preferred_phone: "+65 8456 7890", preferred_email: "sarah.lim@company.sg", preferred_dob: "1995-04-18", preferred_nric: "S9545678D", preferred_address: { address_id: "a-4", unit_number: null, street_number: "22", street_name: "Jurong West Street 41", city: "Singapore", postal_code: "649414", country_code: "SG", normalized_full: "22 Jurong West St 41, Singapore 649414" }, profile_completeness_score: 0.85, golden_profile_computed_at: "2026-05-03T09:00:00Z", golden_profile_version: "3", source_record_count: 5, connection_count: 2, created_at: "2023-11-20T00:00:00Z", updated_at: "2026-05-03T09:00:00Z", phone_confidence: 0.91, entities: [{ entity_key: "eko", display_name: "Eko Commerce", entity_type: "ecommerce", country_code: "SG", is_active: true, source_record_count: 3 }], entity_count: 1, identifier_count: 6, possible_match_count: 0, order_count: 23 },
  { person_id: "p-005", status: "active", is_high_value: true, is_high_risk: false, preferred_full_name: "James Ong Beng Kiat", preferred_phone: "+65 9567 8901", preferred_email: "james.ong@gmail.com", preferred_dob: "1982-09-30", preferred_nric: "S8256789E", preferred_address: { address_id: "a-5", unit_number: "#08-15", street_number: "55", street_name: "Bishan Street 22", city: "Singapore", postal_code: "579804", country_code: "SG", normalized_full: "55 Bishan St 22 #08-15, Singapore 579804" }, profile_completeness_score: 0.95, golden_profile_computed_at: "2026-05-07T06:00:00Z", golden_profile_version: "4", source_record_count: 6, connection_count: 5, created_at: "2023-08-05T00:00:00Z", updated_at: "2026-05-07T06:00:00Z", phone_confidence: 0.99, entities: [{ entity_key: "speedzone", display_name: "SpeedZone Retail", entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 3 }, { entity_key: "fundbox", display_name: "Fundbox Consumer", entity_type: "finance", country_code: "SG", is_active: true, source_record_count: 2 }], entity_count: 2, identifier_count: 7, possible_match_count: 0, order_count: 41 },
  { person_id: "p-006", status: "active", is_high_value: false, is_high_risk: false, preferred_full_name: "Nurul Ain Binte Aziz", preferred_phone: "+65 8678 9012", preferred_email: "nurul.ain@gmail.com", preferred_dob: "1998-02-14", preferred_nric: "T0067890F", preferred_address: null, profile_completeness_score: 0.55, golden_profile_computed_at: "2026-05-02T12:00:00Z", golden_profile_version: "1", source_record_count: 1, connection_count: 0, created_at: "2025-01-10T00:00:00Z", updated_at: "2026-05-02T12:00:00Z", phone_confidence: 0.80, entities: [{ entity_key: "eko", display_name: "Eko Commerce", entity_type: "ecommerce", country_code: "SG", is_active: true, source_record_count: 1 }], entity_count: 1, identifier_count: 2, possible_match_count: 0, order_count: 3 },
  { person_id: "p-007", status: "active", is_high_value: true, is_high_risk: false, preferred_full_name: "David Chen Yong An", preferred_phone: "+65 9789 0123", preferred_email: "david.chen@enterprise.com", preferred_dob: "1975-06-08", preferred_nric: "S7578901G", preferred_address: { address_id: "a-7", unit_number: "#25-01", street_number: "1", street_name: "Marina Bay Financial Centre", city: "Singapore", postal_code: "018981", country_code: "SG", normalized_full: "1 Marina Bay FC #25-01, Singapore 018981" }, profile_completeness_score: 0.97, golden_profile_computed_at: "2026-05-07T07:30:00Z", golden_profile_version: "5", source_record_count: 8, connection_count: 9, created_at: "2023-05-15T00:00:00Z", updated_at: "2026-05-07T07:30:00Z", phone_confidence: 1.0, entities: [{ entity_key: "fundbox", display_name: "Fundbox Consumer", entity_type: "finance", country_code: "SG", is_active: true, source_record_count: 5 }], entity_count: 1, identifier_count: 9, possible_match_count: 0, order_count: 67 },
  { person_id: "p-008", status: "suppressed", is_high_value: false, is_high_risk: true, preferred_full_name: "Kevin Lau Zi Yang", preferred_phone: "+65 8890 1234", preferred_email: null, preferred_dob: null, preferred_nric: null, preferred_address: null, profile_completeness_score: 0.30, golden_profile_computed_at: null, golden_profile_version: null, source_record_count: 1, connection_count: 0, created_at: "2025-03-20T00:00:00Z", updated_at: "2025-03-20T00:00:00Z", phone_confidence: 0.50, entities: [], entity_count: 0, identifier_count: 1, possible_match_count: 0, order_count: 0 },
];

export const MOCK_PERSON_CONNECTIONS_BY_PERSON_ID: Record<string, PersonConnection[]> = {
  "p-001": [
    {
      person_id: "p-002",
      status: "active",
      preferred_full_name: "Priya Ramasamy",
      hops: 1,
      shared_identifiers: [{ identifier_type: "phone", normalized_value: "+6591234567" }],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Emergency contact", relationship_category: "family", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-005",
      status: "active",
      preferred_full_name: "James Ong Beng Kiat",
      hops: 1,
      shared_identifiers: [],
      shared_addresses: [{ address_id: "a-1", normalized_full: "18 Orchard Road #05-12, Singapore 238801", source_system_key: "speedzone_phppos" }],
      knows_relationships: [{ relationship_label: "Referrer", relationship_category: "business", source_system_key: "speedzone_phppos" }],
      connection_sources: [{ source_system_key: "speedzone_phppos", entity_display_name: "SpeedZone Retail" }],
    },
    {
      person_id: "p-007",
      status: "active",
      preferred_full_name: "David Chen Yong An",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Introduced by sales", relationship_category: "business", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
  ],
  "p-002": [
    {
      person_id: "p-001",
      status: "active",
      preferred_full_name: "Tan Wei Ming",
      hops: 1,
      shared_identifiers: [{ identifier_type: "phone", normalized_value: "+6591234567" }],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Emergency contact", relationship_category: "family", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
  ],
  "p-004": [
    {
      person_id: "p-005",
      status: "active",
      preferred_full_name: "James Ong Beng Kiat",
      hops: 1,
      shared_identifiers: [],
      shared_addresses: [{ address_id: "a-4", normalized_full: "22 Jurong West St 41, Singapore 649414", source_system_key: "eko_phppos" }],
      knows_relationships: [{ relationship_label: "Former housemate", relationship_category: "household", source_system_key: "eko_phppos" }],
      connection_sources: [{ source_system_key: "eko_phppos", entity_display_name: "EKO Commerce" }],
    },
    {
      person_id: "p-006",
      status: "active",
      preferred_full_name: "Nurul Ain Binte Aziz",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [],
      connection_sources: [],
    },
  ],
  "p-005": [
    {
      person_id: "p-001",
      status: "active",
      preferred_full_name: "Tan Wei Ming",
      hops: 1,
      shared_identifiers: [],
      shared_addresses: [{ address_id: "a-1", normalized_full: "18 Orchard Road #05-12, Singapore 238801", source_system_key: "speedzone_phppos" }],
      knows_relationships: [{ relationship_label: "Referrer", relationship_category: "business", source_system_key: "speedzone_phppos" }],
      connection_sources: [{ source_system_key: "speedzone_phppos", entity_display_name: "SpeedZone Retail" }],
    },
    {
      person_id: "p-004",
      status: "merged",
      preferred_full_name: "Sarah Lim Hui Ling",
      hops: 1,
      shared_identifiers: [],
      shared_addresses: [{ address_id: "a-4", normalized_full: "22 Jurong West St 41, Singapore 649414", source_system_key: "eko_phppos" }],
      knows_relationships: [{ relationship_label: "Former housemate", relationship_category: "household", source_system_key: "eko_phppos" }],
      connection_sources: [{ source_system_key: "eko_phppos", entity_display_name: "EKO Commerce" }],
    },
    {
      person_id: "p-007",
      status: "active",
      preferred_full_name: "David Chen Yong An",
      hops: 1,
      shared_identifiers: [{ identifier_type: "email", normalized_value: "james.ong@gmail.com" }],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Co-borrower", relationship_category: "finance", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-002",
      status: "active",
      preferred_full_name: "Priya Ramasamy",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Referral lead", relationship_category: "business", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-008",
      status: "suppressed",
      preferred_full_name: "Kevin Lau Zi Yang",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [],
      connection_sources: [],
    },
  ],
  "p-007": [
    {
      person_id: "p-001",
      status: "active",
      preferred_full_name: "Tan Wei Ming",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Introduced by sales", relationship_category: "business", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-002",
      status: "active",
      preferred_full_name: "Priya Ramasamy",
      hops: 1,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Spouse", relationship_category: "family", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-003",
      status: "active",
      preferred_full_name: "Mohamed Faizal Bin Hassan",
      hops: 1,
      shared_identifiers: [{ identifier_type: "phone", normalized_value: "+6593456789" }],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Guarantor", relationship_category: "finance", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-004",
      status: "merged",
      preferred_full_name: "Sarah Lim Hui Ling",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Past employee", relationship_category: "business", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-005",
      status: "active",
      preferred_full_name: "James Ong Beng Kiat",
      hops: 1,
      shared_identifiers: [{ identifier_type: "email", normalized_value: "james.ong@gmail.com" }],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Co-borrower", relationship_category: "finance", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-006",
      status: "active",
      preferred_full_name: "Nurul Ain Binte Aziz",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [{ address_id: "a-7", normalized_full: "1 Marina Bay FC #25-01, Singapore 018981", source_system_key: "speedzone_phppos" }],
      knows_relationships: [{ relationship_label: "Emergency contact", relationship_category: "family", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }, { source_system_key: "speedzone_phppos", entity_display_name: "SpeedZone Retail" }],
    },
    {
      person_id: "p-008",
      status: "suppressed",
      preferred_full_name: "Kevin Lau Zi Yang",
      hops: 1,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Fraud review link", relationship_category: "risk", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
    {
      person_id: "p-009",
      status: "active",
      preferred_full_name: "Alicia Tan Mei Qi",
      hops: 2,
      shared_identifiers: [],
      shared_addresses: [],
      knows_relationships: [],
      connection_sources: [],
    },
    {
      person_id: "p-010",
      status: "active",
      preferred_full_name: "Ravi Kumar s/o Maniam",
      hops: 2,
      shared_identifiers: [{ identifier_type: "phone", normalized_value: "+6597890123" }],
      shared_addresses: [],
      knows_relationships: [{ relationship_label: "Portfolio contact", relationship_category: "business", source_system_key: "fundbox_consumer_backend" }],
      connection_sources: [{ source_system_key: "fundbox_consumer_backend", entity_display_name: "Fundbox Consumer" }],
    },
  ],
};

// ── Entities ──────────────────────────────────────────────────────────────────

export const MOCK_ENTITIES: EntitySummary[] = [
  { entity_key: "speedzone", display_name: "SpeedZone Retail", entity_type: "retail", country_code: "SG", is_active: true, person_count: 1842, source_record_count: 4301, last_ingested_at: "2026-05-07T06:45:00Z", active_review_cases: 4 },
  { entity_key: "fundbox", display_name: "Fundbox Consumer", entity_type: "finance", country_code: "SG", is_active: true, person_count: 3210, source_record_count: 8924, last_ingested_at: "2026-05-07T04:00:00Z", active_review_cases: 7 },
  { entity_key: "eko", display_name: "Eko Commerce", entity_type: "ecommerce", country_code: "SG", is_active: true, person_count: 987, source_record_count: 2103, last_ingested_at: "2026-05-06T22:00:00Z", active_review_cases: 2 },
  { entity_key: "bitrix-sg", display_name: "Bitrix SG CRM", entity_type: "crm", country_code: "SG", is_active: false, person_count: 412, source_record_count: 890, last_ingested_at: "2026-04-15T10:00:00Z", active_review_cases: 0 },
];

// ── Source Systems ────────────────────────────────────────────────────────────

export const MOCK_SOURCE_SYSTEMS: SourceSystemSummary[] = [
  { source_key: "speedzone-phppos", display_name: "SpeedZone POS", system_type: "phppos", is_active: true, source_record_count: 4301, last_ingested_at: "2026-05-07T06:45:00Z" },
  { source_key: "fundbox-bitrix", display_name: "Fundbox Bitrix CRM", system_type: "bitrix", is_active: true, source_record_count: 8924, last_ingested_at: "2026-05-07T04:00:00Z" },
  { source_key: "eko-phppos", display_name: "Eko POS", system_type: "phppos", is_active: true, source_record_count: 2103, last_ingested_at: "2026-05-06T22:00:00Z" },
  { source_key: "whatsapp-main", display_name: "WhatsApp (Main)", system_type: "whatsapp", is_active: true, source_record_count: 1240, last_ingested_at: "2026-05-07T07:10:00Z" },
  { source_key: "bitrix-sg", display_name: "Bitrix SG (Legacy)", system_type: "bitrix", is_active: false, source_record_count: 890, last_ingested_at: "2026-04-15T10:00:00Z" },
];

export const MOCK_SOURCE_SYSTEM_INFOS: SourceSystemInfo[] = [
  { source_system_id: "ss-001", source_key: "speedzone-phppos", display_name: "SpeedZone POS", system_type: "phppos", is_active: true, field_trust: { phone: "tier_1", email: "tier_2", name: "tier_1" }, entity_key: "speedzone", created_at: "2023-01-01T00:00:00Z", updated_at: "2026-05-07T06:45:00Z" },
  { source_system_id: "ss-002", source_key: "fundbox-bitrix", display_name: "Fundbox Bitrix CRM", system_type: "bitrix", is_active: true, field_trust: { phone: "tier_1", email: "tier_1", name: "tier_2", address: "tier_3" }, entity_key: "fundbox", created_at: "2023-01-01T00:00:00Z", updated_at: "2026-05-07T04:00:00Z" },
  { source_system_id: "ss-003", source_key: "eko-phppos", display_name: "Eko POS", system_type: "phppos", is_active: true, field_trust: { phone: "tier_2", email: "tier_3", name: "tier_1" }, entity_key: "eko", created_at: "2023-06-01T00:00:00Z", updated_at: "2026-05-06T22:00:00Z" },
  { source_system_id: "ss-004", source_key: "whatsapp-main", display_name: "WhatsApp (Main)", system_type: "whatsapp", is_active: true, field_trust: { phone: "tier_1", name: "tier_3" }, entity_key: null, created_at: "2024-01-01T00:00:00Z", updated_at: "2026-05-07T07:10:00Z" },
  { source_system_id: "ss-005", source_key: "bitrix-sg", display_name: "Bitrix SG (Legacy)", system_type: "bitrix", is_active: false, field_trust: {}, entity_key: "bitrix-sg", created_at: "2022-01-01T00:00:00Z", updated_at: "2026-04-15T10:00:00Z" },
];

// ── Review Cases ──────────────────────────────────────────────────────────────

export const MOCK_REVIEW_CASES: ReviewCaseSummary[] = [
  { review_case_id: "rc-001", queue_state: "open", priority: 1, assigned_to: null, follow_up_at: null, sla_due_at: "2026-05-07T10:00:00Z", match_decision: { match_decision_id: "md-001", engine_type: "heuristic", decision: "review", confidence: 0.74 } },
  { review_case_id: "rc-002", queue_state: "assigned", priority: 2, assigned_to: "alice@hyperp.sg", follow_up_at: null, sla_due_at: "2026-05-08T14:00:00Z", match_decision: { match_decision_id: "md-002", engine_type: "heuristic", decision: "review", confidence: 0.68 } },
  { review_case_id: "rc-003", queue_state: "open", priority: 3, assigned_to: null, follow_up_at: null, sla_due_at: "2026-05-09T09:00:00Z", match_decision: { match_decision_id: "md-003", engine_type: "deterministic", decision: "review", confidence: 0.81 } },
  { review_case_id: "rc-004", queue_state: "open", priority: 5, assigned_to: null, follow_up_at: "2026-05-10T00:00:00Z", sla_due_at: "2026-05-12T17:00:00Z", match_decision: { match_decision_id: "md-004", engine_type: "heuristic", decision: "review", confidence: 0.62 } },
  { review_case_id: "rc-005", queue_state: "deferred", priority: 4, assigned_to: "bob@hyperp.sg", follow_up_at: "2026-05-08T09:00:00Z", sla_due_at: null, match_decision: { match_decision_id: "md-005", engine_type: "llm", decision: "review", confidence: 0.70 } },
  { review_case_id: "rc-006", queue_state: "resolved", priority: 2, assigned_to: "alice@hyperp.sg", follow_up_at: null, sla_due_at: null, match_decision: { match_decision_id: "md-006", engine_type: "heuristic", decision: "merge", confidence: 0.88 } },
  { review_case_id: "rc-007", queue_state: "open", priority: 6, assigned_to: null, follow_up_at: null, sla_due_at: "2026-05-14T17:00:00Z", match_decision: { match_decision_id: "md-007", engine_type: "deterministic", decision: "review", confidence: 0.77 } },
];

export const MOCK_REVIEW_DETAIL: ReviewCaseDetail = {
  review_case_id: "rc-001",
  queue_state: "open",
  priority: 1,
  assigned_to: null,
  follow_up_at: null,
  sla_due_at: "2026-05-07T10:00:00Z",
  resolution: null,
  resolved_at: null,
  actions: [
    { action_type: "system_created", actor_type: "system", actor_id: "match-engine", notes: "Auto-escalated due to high confidence conflict", created_at: "2026-05-06T08:00:00Z" },
  ],
  match_decision: {
    match_decision_id: "md-001",
    engine_type: "heuristic",
    engine_version: "1.3.0",
    policy_version: "2026-Q1",
    decision: "review",
    confidence: 0.74,
    reasons: ["phone_match_normalized", "name_similarity_high", "address_partial_match"],
    blocking_conflicts: [],
    created_at: "2026-05-06T08:00:00Z",
    left_person_id: "p-001",
    right_person_id: "p-002",
  },
  comparison_left: {
    entity_kind: "person",
    person_id: "p-001",
    source_record_pk: null,
    source_record_id: null,
    status: "active",
    preferred_full_name: "Tan Wei Ming",
    preferred_phone: "+65 9123 4567",
    preferred_email: "weiming.tan@gmail.com",
    preferred_dob: "1985-03-12",
    preferred_address: { address_id: "a-1", unit_number: "#05-12", street_number: "18", street_name: "Orchard Road", city: "Singapore", postal_code: "238801", country_code: "SG", normalized_full: "18 Orchard Road #05-12, Singapore 238801" },
  },
  comparison_right: {
    entity_kind: "source_record",
    person_id: null,
    source_record_pk: "sr-999",
    source_record_id: "SR-2024-09981",
    status: null,
    preferred_full_name: "Tan W.M.",
    preferred_phone: "+65 9123 4567",
    preferred_email: "wm.tan@gmail.com",
    preferred_dob: "1985-03-12",
    preferred_address: null,
  },
  created_at: "2026-05-06T08:00:00Z",
  updated_at: "2026-05-06T08:00:00Z",
};

// ── Ingestion Runs ────────────────────────────────────────────────────────────

export const MOCK_INGEST_RUNS: IngestRunDetailResponse[] = [
  { ingest_run_id: "run-001", run_type: "batch", status: "completed", record_count: 1240, rejected_count: 3, started_at: "2026-05-07T06:30:00Z", finished_at: "2026-05-07T06:45:00Z", source_key: "speedzone-phppos" },
  { ingest_run_id: "run-002", run_type: "batch", status: "completed", record_count: 3410, rejected_count: 12, started_at: "2026-05-07T03:45:00Z", finished_at: "2026-05-07T04:00:00Z", source_key: "fundbox-bitrix" },
  { ingest_run_id: "run-003", run_type: "realtime", status: "running", record_count: 87, rejected_count: 0, started_at: "2026-05-07T07:05:00Z", finished_at: null, source_key: "whatsapp-main" },
  { ingest_run_id: "run-004", run_type: "batch", status: "failed", record_count: 0, rejected_count: 0, started_at: "2026-05-06T22:00:00Z", finished_at: "2026-05-06T22:01:00Z", source_key: "eko-phppos" },
  { ingest_run_id: "run-005", run_type: "batch", status: "completed", record_count: 890, rejected_count: 1, started_at: "2026-05-06T18:00:00Z", finished_at: "2026-05-06T18:12:00Z", source_key: "speedzone-phppos" },
];

// ── Events ────────────────────────────────────────────────────────────────────

export const MOCK_EVENTS: DownstreamEvent[] = [
  { event_id: "ev-001", event_type: "merge_completed", affected_person_ids: ["p-004", "p-009"], metadata: { merge_event_id: "me-001", engine: "deterministic", confidence: "0.96" }, created_at: "2026-05-07T07:20:00Z" },
  { event_id: "ev-002", event_type: "profile_updated", affected_person_ids: ["p-001"], metadata: { source: "speedzone-phppos", fields_changed: "phone,address" }, created_at: "2026-05-07T06:48:00Z" },
  { event_id: "ev-003", event_type: "merge_completed", affected_person_ids: ["p-005", "p-011"], metadata: { merge_event_id: "me-002", engine: "heuristic", confidence: "0.83" }, created_at: "2026-05-07T04:05:00Z" },
  { event_id: "ev-004", event_type: "review_resolved", affected_person_ids: ["p-002"], metadata: { review_case_id: "rc-006", action: "merge", actor: "alice@hyperp.sg" }, created_at: "2026-05-06T16:30:00Z" },
  { event_id: "ev-005", event_type: "ingestion_completed", affected_person_ids: [], metadata: { source_key: "fundbox-bitrix", run_id: "run-002", records: "3410" }, created_at: "2026-05-07T04:00:00Z" },
  { event_id: "ev-006", event_type: "profile_updated", affected_person_ids: ["p-007"], metadata: { source: "fundbox-bitrix", fields_changed: "email" }, created_at: "2026-05-06T22:15:00Z" },
  { event_id: "ev-007", event_type: "suppression_applied", affected_person_ids: ["p-008"], metadata: { reason: "duplicate_high_risk", actor: "system" }, created_at: "2026-05-06T11:00:00Z" },
];

// ── Reports ───────────────────────────────────────────────────────────────────

export const MOCK_REPORTS: ReportSummary[] = [
  { report_key: "high-value-customers", display_name: "High-Value Customers", description: "Customers ranked by total spend across all entities.", category: "Sales" },
  { report_key: "contact-tracing", display_name: "Contact Tracing", description: "Multi-hop relationship traversal for a given person.", category: "Compliance" },
  { report_key: "unresolved-duplicates", display_name: "Unresolved Duplicates", description: "Persons with pending review cases older than 7 days.", category: "Data Quality" },
  { report_key: "source-coverage", display_name: "Source Coverage", description: "Breakdown of persons per source system and entity.", category: "Operations" },
];

export const MOCK_REPORT_DETAIL: ReportDetail = {
  report_key: "high-value-customers",
  display_name: "High-Value Customers",
  description: "Customers ranked by total spend across all entities.",
  category: "Sales",
  cypher_query: "MATCH (p:Person) WHERE p.is_high_value = true RETURN p.person_id, p.preferred_full_name, p.connection_count ORDER BY p.connection_count DESC LIMIT $limit",
  parameters: [{ name: "limit", label: "Max results", param_type: "integer", required: false, default_value: "50" }],
  created_at: "2025-01-01T00:00:00Z",
  updated_at: "2026-04-01T00:00:00Z",
};

// ── Users ─────────────────────────────────────────────────────────────────────

export const MOCK_USERS: UserResponse[] = [
  { email: "admin@hyperp.sg", google_sub: "sub-001", role: "admin", entity_key: null, display_name: "Admin User" },
  { email: "alice@hyperp.sg", google_sub: "sub-002", role: "employee", entity_key: "speedzone", display_name: "Alice Tan" },
  { email: "bob@hyperp.sg", google_sub: "sub-003", role: "employee", entity_key: "fundbox", display_name: "Bob Lim" },
  { email: "charlie@hyperp.sg", google_sub: "sub-004", role: "employee", entity_key: "eko", display_name: "Charlie Ng" },
  { email: "new.user@company.sg", google_sub: null, role: "first_time", entity_key: null, display_name: null },
];

// ── OAuth Clients ─────────────────────────────────────────────────────────────

export const MOCK_OAUTH_CLIENTS: OAuthClient[] = [
  {
    client_id: "cli-001",
    name: "SpeedZone POS Integration",
    entity_key: "speedzone",
    scopes: ["ingest:write", "persons:read"],
    created_by: "admin@hyperp.sg",
    created_at: "2025-03-01T00:00:00Z",
    disabled_at: null,
    last_used_at: "2026-05-07T06:30:00Z",
    secrets: [{ secret_id: "sec-001", secret_prefix: "hps_prod_", created_at: "2025-03-01T00:00:00Z", expires_at: null, revoked_at: null, last_used_at: "2026-05-07T06:30:00Z" }],
  },
  {
    client_id: "cli-002",
    name: "Analytics Dashboard",
    entity_key: null,
    scopes: ["persons:read"],
    created_by: "admin@hyperp.sg",
    created_at: "2025-06-15T00:00:00Z",
    disabled_at: null,
    last_used_at: "2026-05-06T14:00:00Z",
    secrets: [{ secret_id: "sec-002", secret_prefix: "hps_anal_", created_at: "2025-06-15T00:00:00Z", expires_at: "2027-06-15T00:00:00Z", revoked_at: null, last_used_at: "2026-05-06T14:00:00Z" }],
  },
  {
    client_id: "cli-003",
    name: "Legacy Bitrix Connector",
    entity_key: "bitrix-sg",
    scopes: ["ingest:write"],
    created_by: "bob@hyperp.sg",
    created_at: "2024-01-10T00:00:00Z",
    disabled_at: "2026-04-01T00:00:00Z",
    last_used_at: "2026-03-30T00:00:00Z",
    secrets: [],
  },
];

// ── Person Detail: Identifiers ────────────────────────────────────────────────

export const MOCK_IDENTIFIERS: PersonIdentifier[] = [
  { identifier_type: "phone",  normalized_value: "+6591234567",           is_active: true,  is_verified: true,  last_confirmed_at: "2026-05-06T08:00:00Z", source_system_key: "speedzone_phppos", source_record_ids: ["SPZ-CUST-00123"],      entities: [{ entity_key: "speedzone", display_name: "SpeedZone Retail",  entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 1 }], source_records: [] },
  { identifier_type: "phone",  normalized_value: "+6587654321",           is_active: false, is_verified: false, last_confirmed_at: "2026-04-18T09:30:00Z", source_system_key: "whatsapp_chat",    source_record_ids: ["WA-2026-05-001"],       entities: [], source_records: [] },
  { identifier_type: "email",  normalized_value: "weiming.tan@gmail.com", is_active: true,  is_verified: true,  last_confirmed_at: "2026-05-01T00:00:00Z", source_system_key: "fundbox_consumer_backend", source_record_ids: ["BITRIX-CONTACT-8821"], entities: [{ entity_key: "fundbox", display_name: "Fundbox Consumer",   entity_type: "crm",    country_code: "SG", is_active: true, source_record_count: 1 }], source_records: [] },
  { identifier_type: "email",  normalized_value: "wm.tan@company.sg",     is_active: false, is_verified: false, last_confirmed_at: null,                   source_system_key: "speedzone_phppos", source_record_ids: ["BITRIX-CONTACT-8821"], entities: [{ entity_key: "speedzone", display_name: "SpeedZone Retail",  entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 1 }], source_records: [] },
  { identifier_type: "nric",   normalized_value: "S8512345C",             is_active: true,  is_verified: true,  last_confirmed_at: "2024-01-10T00:00:00Z", source_system_key: "fundbox_consumer_backend", source_record_ids: ["BITRIX-CONTACT-8821"], entities: [{ entity_key: "fundbox", display_name: "Fundbox Consumer",   entity_type: "crm",    country_code: "SG", is_active: true, source_record_count: 1 }], source_records: [] },
  { identifier_type: "member", normalized_value: "SPZ-00123",             is_active: true,  is_verified: false, last_confirmed_at: "2025-08-15T00:00:00Z", source_system_key: "speedzone_phppos", source_record_ids: ["SPZ-CUST-00123"],      entities: [{ entity_key: "speedzone", display_name: "SpeedZone Retail",  entity_type: "retail", country_code: "SG", is_active: true, source_record_count: 1 }], source_records: [] },
];

// ── Person Detail: Source Records ────────────────────────────────────────────

export const MOCK_SOURCE_RECORDS: PersonSourceRecord[] = [
  { source_record_pk: "sr-001", source_system: "speedzone-phppos", entity_key: "speedzone-retail",   entity_display_name: "SpeedZone Retail",  source_record_id: "SPZ-CUST-00123",      source_record_version: "3",  record_type: "system",       extraction_method: null, extraction_confidence: null, link_status: "linked", linked_person_id: "p-001", observed_at: "2026-05-06T08:00:00Z", ingested_at: "2026-05-07T06:45:00Z", observed_at_display: "06 May 2026, 08:00 AM", ingested_at_display: "07 May 2026, 06:45 AM", extraction_confidence_display: null, chat_transcript: null, conversation_ref: null, raw_payload: { customer_id: "00123", mobile: "91234567", nric: "S8512345A", full_name: "Tan Wei Ming" }, normalized_payload: { identifiers: [{ identifier_type: "phone", normalized_value: "+6591234567", is_verified: true }, { identifier_type: "nric", normalized_value: "S8512345A", is_verified: true }], address: { normalized_full: "18 Orchard Road #05-12, Singapore 238801", unit_number: "#05-12", street_number: "18", street_name: "Orchard Road", city: "Singapore", postal_code: "238801", country_code: "SG", quality_flag: "full_parse" }, attributes: [{ attribute_name: "full_name", attribute_value: "Tan Wei Ming" }, { attribute_name: "dob", attribute_value: "1985-03-12" }, { attribute_name: "gender", attribute_value: "MALE" }, { attribute_name: "nationality", attribute_value: "Singapore Citizen" }] } },
  { source_record_pk: "sr-002", source_system: "fundbox-bitrix",  entity_key: "fundbox-consumer",   entity_display_name: "Fundbox Consumer",   source_record_id: "BITRIX-CONTACT-8821", source_record_version: "1",  record_type: "system",       extraction_method: null, extraction_confidence: null, link_status: "linked", linked_person_id: "p-001", observed_at: "2026-05-01T00:00:00Z", ingested_at: "2026-05-07T04:00:00Z", observed_at_display: "01 May 2026, 12:00 AM", ingested_at_display: "07 May 2026, 04:00 AM", extraction_confidence_display: null, chat_transcript: null, conversation_ref: null, raw_payload: { contact_id: "8821", emails: ["weiming.tan@gmail.com", "wm.tan@company.sg"], name: "Wei Ming Tan" }, normalized_payload: { identifiers: [{ identifier_type: "email", normalized_value: "weiming.tan@gmail.com", is_verified: true }, { identifier_type: "email", normalized_value: "wm.tan@company.sg", is_verified: false }], address: { normalized_full: "301 Alexandra Road, Singapore 159968", unit_number: null, street_number: "301", street_name: "Alexandra Road", city: "Singapore", postal_code: "159968", country_code: "SG", quality_flag: "partial_parse" }, attributes: [{ attribute_name: "full_name", attribute_value: "Wei Ming Tan" }, { attribute_name: "company", attribute_value: "Self-employed" }] } },
  { source_record_pk: "sr-003", source_system: "whatsapp-main",   entity_key: null,                 entity_display_name: null,                 source_record_id: "WA-2026-05-001",      source_record_version: null, record_type: "conversation", extraction_method: "llm-gpt4o", extraction_confidence: 0.82, link_status: "linked", linked_person_id: "p-001", observed_at: "2026-05-03T14:30:00Z", ingested_at: "2026-05-07T07:10:00Z", observed_at_display: "03 May 2026, 02:30 PM", ingested_at_display: "07 May 2026, 07:10 AM", extraction_confidence_display: "82%", chat_transcript: [{ timestamp: "2026-05-03 14:30:00", timestamp_display: "03 May 2026, 02:30 PM", speaker: "Wei Ming", phone: "+6591234567", role: "customer", text: "Hi, I'd like to upgrade my membership" }, { timestamp: "2026-05-03 14:31:00", timestamp_display: "03 May 2026, 02:31 PM", speaker: "Eko Life (Main)", phone: null, role: "agent", text: "Sure! Let me pull up your account." }], conversation_ref: { channel: "whatsapp", message_id: "WA-2026-05-001" }, raw_payload: { messages_text: "[2026-05-03 14:30:00] Wei Ming (+6591234567): Hi, I'd like to upgrade my membership\n[2026-05-03 14:31:00] Eko Life (Main): Sure! Let me pull up your account.", chat_members: [{ name: "Wei Ming", phone: "+6591234567", role: "customer" }, { name: "Eko Life (Main)", phone: "", role: "agent" }] }, normalized_payload: { identifiers: [{ identifier_type: "phone", normalized_value: "+6591234567", is_verified: false }, { identifier_type: "phone", normalized_value: "+6587654321", is_verified: false }], address: null, attributes: [{ attribute_name: "mentioned_name", attribute_value: "Wei Ming" }], summary: "Customer enquired about membership upgrade via WhatsApp." } },
];

// ── Person Detail: Sales ──────────────────────────────────────────────────────

export const MOCK_SALES: SalesOrder[] = [
  { order_no: "SPZ-ORD-20260507", source_order_id: "POS-9981", order_date: "2026-05-07", release_date: "2026-05-07", total_amount: 248.90, currency: "SGD", source_system: "speedzone-phppos", entity_name: "SpeedZone Retail", line_items: [{ line_no: 1, quantity: 1, unit_price: 189.00, subtotal: 189.00, product: { display_name: "Nike Air Max 270", sku: "NK-AM270-BLK-42", category: "Footwear" } }, { line_no: 2, quantity: 2, unit_price: 29.95, subtotal: 59.90, product: { display_name: "Sports Socks (3-pack)", sku: "ACC-SOCK-WH-L", category: "Accessories" } }] },
  { order_no: "SPZ-ORD-20260312", source_order_id: "POS-8812", order_date: "2026-03-12", release_date: "2026-03-12", total_amount: 159.00, currency: "SGD", source_system: "speedzone-phppos", entity_name: "SpeedZone Retail", line_items: [{ line_no: 1, quantity: 1, unit_price: 159.00, subtotal: 159.00, product: { display_name: "Adidas Ultraboost 22", sku: "AD-UB22-GRY-42", category: "Footwear" } }] },
  { order_no: "FB-INV-2026-0041", source_order_id: "INV-0041", order_date: "2026-02-14", release_date: "2026-02-15", total_amount: 3200.00, currency: "SGD", source_system: "fundbox-bitrix", entity_name: "Fundbox Consumer", line_items: [{ line_no: 1, quantity: 1, unit_price: 3200.00, subtotal: 3200.00, product: { display_name: "Personal Loan Processing Fee", sku: "FIN-LOAN-PROC", category: "Financial Services" } }] },
];

// ── Person Detail: Audit ──────────────────────────────────────────────────────

export const MOCK_AUDIT: PersonAuditEvent[] = [
  { merge_event_id: "me-010", event_type: "profile_recomputed", actor_type: "system", actor_id: "golden-profile-service", reason: "Source record updated from speedzone-phppos", metadata: { trigger: "ingestion", source_key: "speedzone-phppos" }, created_at: "2026-05-07T06:45:00Z", absorbed_person_id: null, survivor_person_id: null, triggered_by_decision_id: null },
  { merge_event_id: "me-008", event_type: "merge_completed",    actor_type: "engine", actor_id: "deterministic-v1.3", reason: "Exact phone match confirmed", metadata: { confidence: "0.96", engine: "deterministic" }, created_at: "2026-03-15T10:00:00Z", absorbed_person_id: "p-099", survivor_person_id: "p-001", triggered_by_decision_id: "md-088" },
  { merge_event_id: "me-005", event_type: "record_linked",      actor_type: "system", actor_id: "ingestion-service",   reason: null, metadata: { source_record_pk: "sr-003", source: "whatsapp-main" }, created_at: "2026-05-07T07:10:00Z", absorbed_person_id: null, survivor_person_id: null, triggered_by_decision_id: null },
  { merge_event_id: "me-001", event_type: "person_created",     actor_type: "system", actor_id: "ingestion-service",   reason: "First record ingested", metadata: { source: "speedzone-phppos" }, created_at: "2024-01-10T00:00:00Z", absorbed_person_id: null, survivor_person_id: null, triggered_by_decision_id: null },
];

// ── Person Detail: Bankruptcy ─────────────────────────────────────────────────

export const MOCK_BANKRUPTCY: PersonBankruptcyCase[] = [
  {
    bankruptcy_case_id: "bk-001",
    source_system_key: "ipto-sg",
    source_case_id: "B-2891/2019",
    case_number: "B-2891/2019",
    document_type: "bankruptcy_order",
    document_date: "2019-03-15",
    event_type: "discharged",
    event_date: "2022-08-10",
    trustee_name: "Official Assignee",
    trustee_firm: "Ministry of Law",
    source_url: null,
    first_seen_at: "2024-01-10T00:00:00Z",
    last_seen_at: "2026-05-01T00:00:00Z",
    created_at: "2024-01-10T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
  },
];

// ── Graph ─────────────────────────────────────────────────────────────────────

export const MOCK_GRAPH: PersonGraph = {
  nodes: [
    { id: "p-001", label: "Person", properties: { preferred_full_name: "Tan Wei Ming", status: "active" } },
    { id: "p-002", label: "Person", properties: { preferred_full_name: "Priya Ramasamy", status: "active" } },
    { id: "p-005", label: "Person", properties: { preferred_full_name: "James Ong Beng Kiat", status: "active" } },
    { id: "p-007", label: "Person", properties: { preferred_full_name: "David Chen Yong An", status: "active" } },
    { id: "id-phone-1", label: "Identifier", properties: { identifier_type: "phone", normalized_value: "+6591234567" } },
    { id: "id-phone-2", label: "Identifier", properties: { identifier_type: "phone", normalized_value: "+6595678901" } },
    { id: "addr-001", label: "Address", properties: { normalized_full: "18 Orchard Road #05-12" } },
  ],
  edges: [
    { id: "e-001", source: "p-001", target: "id-phone-1", type: "IDENTIFIED_BY", properties: {} },
    { id: "e-002", source: "p-002", target: "id-phone-1", type: "IDENTIFIED_BY", properties: {} },
    { id: "e-003", source: "p-005", target: "id-phone-2", type: "IDENTIFIED_BY", properties: {} },
    { id: "e-004", source: "p-001", target: "addr-001", type: "LIVES_AT", properties: {} },
    { id: "e-005", source: "p-005", target: "addr-001", type: "LIVES_AT", properties: {} },
    { id: "e-006", source: "p-001", target: "p-007", type: "KNOWS", properties: { relationship_label: "referrer" } },
  ],
};
