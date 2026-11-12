use serde::{Deserialize, Serialize};

#[derive(Clone, Deserialize, Serialize)]
pub struct AddressSummary {
    pub address_id: String,
    pub unit_number: Option<String>,
    pub street_number: Option<String>,
    pub street_name: Option<String>,
    pub city: Option<String>,
    pub postal_code: Option<String>,
    pub country_code: Option<String>,
    pub normalized_full: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct LoyaltySummary {
    pub source_system: String,
    pub points: Option<i64>,
    pub disable_loyalty: Option<bool>,
    pub current_spend_for_points: Option<f64>,
    pub current_sales_for_discount: Option<f64>,
    pub observed_at: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct VehicleSummary {
    pub vehicle_id: String,
    pub product: Option<String>,
    pub product_sku: Option<String>,
    pub manufacturer: Option<String>,
    pub model: Option<String>,
    pub lta_tag: Option<String>,
    pub serial_number: Option<String>,
    pub relationship: String,
    pub is_active: Option<bool>,
    pub conflict_flag: Option<bool>,
    pub observed_at: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct Person {
    pub person_id: String,
    pub status: String,
    pub is_high_value: bool,
    pub is_high_risk: bool,
    pub preferred_full_name: Option<String>,
    pub preferred_phone: Option<String>,
    pub preferred_email: Option<String>,
    pub preferred_dob: Option<String>,
    pub preferred_address: Option<AddressSummary>,
    pub preferred_nric: Option<String>,
    pub preferred_race_ethnicity: Option<String>,
    pub profile_completeness_score: f64,
    pub golden_profile_computed_at: Option<String>,
    pub golden_profile_version: Option<String>,
    pub source_record_count: i64,
    pub connection_count: i64,
    pub lifetime_value: Option<f64>,
    pub loyalty: Option<Vec<LoyaltySummary>>,
    pub vehicles: Option<Vec<VehicleSummary>>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct PersonEntitySummary {
    pub entity_key: String,
    pub display_name: Option<String>,
    pub entity_type: Option<String>,
    pub country_code: Option<String>,
    pub is_active: bool,
    pub source_record_count: i64,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct ListedPerson {
    #[serde(flatten)]
    pub person: Person,
    pub phone_confidence: Option<f64>,
    pub entities: Vec<PersonEntitySummary>,
    pub entity_count: i64,
    pub identifier_count: i64,
    pub possible_match_count: i64,
    pub system_match_count: i64,
    pub order_count: i64,
    pub crm_deal_count: i64,
    pub bankruptcy_case_count: i64,
    pub preferred_dob_display: String,
    pub preferred_dob_invalid: bool,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct PersonListSummary {
    pub all_profiles_count: i64,
    pub high_risk_count: i64,
    pub high_value_count: i64,
    pub no_contact_count: i64,
    pub deals_this_month_count: i64,
    pub all_deals_count: i64,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct IdentityLinkRevision {
    pub event_id: String,
    pub global_revision: i64,
    pub source_system: String,
    pub source_instance_id: String,
    pub source_entity_type: String,
    pub source_entity_id: String,
    pub identity_policy_version: String,
    pub link_status: String,
    pub hyperp_person_id: Option<String>,
    pub resolution_kind: String,
    pub resolution_revision: i64,
    pub effective_at: String,
    pub match_decision_id: Option<String>,
    pub review_case_id: Option<String>,
    pub supersedes_event_id: Option<String>,
}

#[derive(Deserialize, Serialize)]
pub struct ResponseMeta {
    pub request_id: String,
    pub next_cursor: Option<String>,
    pub total_count: Option<i64>,
}

#[derive(Deserialize, Serialize)]
pub struct ApiResponse<T> {
    pub data: T,
    pub meta: ResponseMeta,
    pub display_items: Option<()>,
}

impl<T: Serialize> ApiResponse<T> {
    pub fn new(
        data: T,
        request_id: String,
        next_cursor: Option<String>,
        total_count: Option<i64>,
    ) -> Self {
        Self {
            data,
            meta: ResponseMeta {
                request_id,
                next_cursor,
                total_count,
            },
            display_items: None,
        }
    }
}

#[derive(Deserialize, Serialize)]
pub struct IdentityPage<T> {
    pub data: Vec<T>,
    pub meta: ResponseMeta,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub through_revision: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snapshot_revision: Option<i64>,
}
