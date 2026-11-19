use axum::{
    extract::{Path, RawQuery, State},
    http::{HeaderMap, StatusCode},
    Json,
};
use chrono::{DateTime, NaiveDate, NaiveDateTime, Utc};
use serde_json::Value;
use sqlx::{Postgres, QueryBuilder, Row};

use crate::{
    contracts::{ApiResponse, ListedPerson, Person, PersonListSummary},
    cursors::{decode_offset, encode_offset},
    error::{request_id, ApiError},
    params::Params,
    AppState,
};

const LIST_PARAMS: &[&str] = &[
    "entity_key",
    "entity_key_mode",
    "source_key",
    "source_key_mode",
    "source_record_type",
    "is_high_value",
    "is_high_risk",
    "has_phone",
    "has_email",
    "has_any_contact",
    "has_address",
    "has_bankruptcy_case",
    "has_any_match",
    "has_possible_match",
    "has_system_match",
    "crm_deal_count_min",
    "crm_deal_count_max",
    "addr_street",
    "addr_unit",
    "addr_city",
    "addr_postal",
    "addr_country",
    "updated_after",
    "updated_before",
    "has_dob",
    "dob_from",
    "dob_to",
    "dob_year",
    "dob_month",
    "dob_day",
    "q",
    "sort_by",
    "sort_order",
    "cursor",
    "limit",
    "include_total",
];

async fn ready(state: &AppState, id: &str) -> Result<(), ApiError> {
    let found = sqlx::query_scalar::<_, bool>(
        "SELECT last_success IS NOT NULL FROM summary_counter WHERE id = 'global'",
    )
    .fetch_optional(&state.pool)
    .await
    .map_err(|_| ApiError::database(id.into()))?;
    if found != Some(true) {
        return Err(ApiError::database(id.into()));
    }
    Ok(())
}

pub async fn summary(
    State(state): State<AppState>,
    headers: HeaderMap,
    RawQuery(raw): RawQuery,
) -> Result<Json<ApiResponse<PersonListSummary>>, ApiError> {
    let id = request_id(&headers);
    Params::parse(raw.as_deref(), &[], &[]).map_err(|_| ApiError::invalid(id.clone()))?;
    let value = sqlx::query_scalar::<_, Value>("SELECT summary FROM summary_counter WHERE id = 'global' AND last_success IS NOT NULL AND summary IS NOT NULL")
        .fetch_optional(&state.pool).await.map_err(|_| ApiError::database(id.clone()))?
        .ok_or_else(|| ApiError::database(id.clone()))?;
    let summary = serde_json::from_value(value).map_err(|_| ApiError::database(id.clone()))?;
    Ok(Json(ApiResponse::new(summary, id, None, None)))
}

pub async fn detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(person_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Json<ApiResponse<Person>>, ApiError> {
    let id = request_id(&headers);
    Params::parse(raw.as_deref(), &[], &[]).map_err(|_| ApiError::invalid(id.clone()))?;
    ready(&state, &id).await?;
    let value = sqlx::query_scalar::<_, Value>("SELECT coalesce(c.detail_payload, p.detail_payload) FROM person p LEFT JOIN person c ON c.person_id = p.canonical_id WHERE p.person_id = $1")
        .bind(person_id).fetch_optional(&state.pool).await.map_err(|_| ApiError::database(id.clone()))?
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "person_not_found", "Person not found.", id.clone()))?;
    let person = serde_json::from_value(value).map_err(|_| ApiError::database(id.clone()))?;
    Ok(Json(ApiResponse::new(person, id, None, None)))
}

fn sort_column(value: &str) -> Option<&'static str> {
    match value {
        "preferred_full_name" => Some("p.preferred_full_name"),
        "preferred_phone" => Some("p.preferred_phone"),
        "preferred_email" => Some("p.preferred_email"),
        "preferred_dob" => Some("p.preferred_dob"),
        "preferred_nric" => Some("p.preferred_nric"),
        "profile_completeness_score" => Some("p.profile_completeness_score"),
        "updated_at" => Some("p.updated_at"),
        "source_record_count" => Some("p.source_record_count"),
        "connection_count" => Some("p.connection_count"),
        "entity_count" => Some("p.entity_count"),
        "possible_match_count" => Some("p.possible_match_count"),
        "system_match_count" => Some("p.system_match_count"),
        "order_count" => Some("p.order_count"),
        "crm_deal_count" => Some("p.crm_deal_count"),
        "bankruptcy_case_count" => Some("p.bankruptcy_case_count"),
        "phone_confidence" => Some("p.phone_confidence"),
        "relevance" => Some("relevance"),
        _ => None,
    }
}

fn date_time(value: &str) -> Result<DateTime<Utc>, ()> {
    if let Ok(value) = DateTime::parse_from_rfc3339(value) {
        return Ok(value.with_timezone(&Utc));
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(value, format) {
            return Ok(parsed.and_utc());
        }
    }
    let date = NaiveDate::parse_from_str(value, "%Y-%m-%d").map_err(|_| ())?;
    Ok(date.and_hms_opt(0, 0, 0).ok_or(())?.and_utc())
}

fn filters(
    builder: &mut QueryBuilder<'_, Postgres>,
    params: &Params,
    sort: &str,
    has_q: bool,
) -> Result<(), ()> {
    builder.push(" WHERE p.status <> 'merged'");
    if !has_q && sort == "profile_completeness_score" {
        builder.push(" AND p.profile_completeness_score IS NOT NULL");
    }
    for (key, expression) in [
        ("is_high_value", "p.is_high_value"),
        ("is_high_risk", "p.is_high_risk"),
        ("has_phone", "(p.preferred_phone IS NOT NULL)"),
        ("has_email", "(p.preferred_email IS NOT NULL)"),
        (
            "has_any_contact",
            "(p.preferred_phone IS NOT NULL OR p.preferred_email IS NOT NULL)",
        ),
        ("has_address", "(p.preferred_address_id IS NOT NULL)"),
        ("has_dob", "(p.preferred_dob IS NOT NULL)"),
        ("has_bankruptcy_case", "(p.bankruptcy_case_count > 0)"),
        ("has_possible_match", "(p.possible_match_count > 0)"),
        ("has_system_match", "(p.system_match_count > 0)"),
        (
            "has_any_match",
            "(p.possible_match_count > 0 OR p.system_match_count > 0)",
        ),
    ] {
        if let Some(value) = params.boolean(key)? {
            builder
                .push(" AND ")
                .push(expression)
                .push(" = ")
                .push_bind(value);
        }
    }
    for (key, op) in [
        ("crm_deal_count_min", " >= "),
        ("crm_deal_count_max", " <= "),
    ] {
        if let Some(value) = params.integer(key)? {
            if value < 0 {
                return Err(());
            }
            builder
                .push(" AND p.crm_deal_count")
                .push(op)
                .push_bind(value);
        }
    }
    let min = params.integer("crm_deal_count_min")?;
    let max = params.integer("crm_deal_count_max")?;
    if min.zip(max).is_some_and(|(min, max)| min > max) {
        return Err(());
    }
    for (key, op) in [("updated_after", " >= "), ("updated_before", " <= ")] {
        if let Some(value) = params.get(key) {
            builder
                .push(" AND p.updated_at")
                .push(op)
                .push_bind(date_time(value)?);
        }
    }
    for (key, expression, op) in [
        ("dob_from", "p.preferred_dob", " >= "),
        ("dob_to", "p.preferred_dob", " <= "),
        ("dob_year", "substring(p.preferred_dob, 1, 4)", " = "),
        ("dob_month", "substring(p.preferred_dob, 6, 2)", " = "),
        ("dob_day", "substring(p.preferred_dob, 9, 2)", " = "),
    ] {
        if let Some(value) = params.get(key).filter(|value| !value.is_empty()) {
            let value = if key == "dob_month" || key == "dob_day" {
                value
                    .trim()
                    .parse::<i32>()
                    .map(|v| format!("{v:02}"))
                    .unwrap_or_else(|_| value.trim().into())
            } else {
                value.to_owned()
            };
            builder
                .push(" AND ")
                .push(expression)
                .push(op)
                .push_bind(value);
        }
    }
    let address_filters = [
        ("addr_street", "street_name"),
        ("addr_unit", "unit_number"),
        ("addr_city", "city"),
        ("addr_postal", "postal_code"),
        ("addr_country", "country_code"),
    ];
    if address_filters
        .iter()
        .any(|(key, _)| params.get(key).is_some())
    {
        builder.push(" AND EXISTS (SELECT 1 FROM person_address a WHERE a.person_id = p.person_id AND coalesce(a.is_active, true)");
        for (key, column) in address_filters {
            if let Some(value) = params.get(key) {
                builder
                    .push(" AND strpos(lower(a.")
                    .push(column)
                    .push("), lower(")
                    .push_bind(value.to_owned())
                    .push(")) > 0");
            }
        }
        builder.push(")");
    }
    for (key, mode_key, column) in [
        ("entity_key", "entity_key_mode", "entity_key"),
        ("source_key", "source_key_mode", "source_key"),
    ] {
        let mode = params.get(mode_key).unwrap_or("or");
        if mode != "or" && mode != "and" {
            return Err(());
        }
        let values = params.list(key);
        if values.is_empty() {
            continue;
        }
        let join = if mode == "and" { " AND " } else { " OR " };
        builder.push(" AND (");
        for (index, value) in values.iter().enumerate() {
            if index > 0 {
                builder.push(join);
            }
            builder.push("EXISTS (SELECT 1 FROM source_record s WHERE s.person_id = p.person_id AND coalesce(s.is_active, true) AND s.lifecycle_status = 'active' AND s.").push(column).push(" = ").push_bind(value.clone()).push(")");
        }
        builder.push(")");
    }
    if let Some(value) = params.get("source_record_type") {
        builder.push(" AND EXISTS (SELECT 1 FROM source_record s WHERE s.person_id = p.person_id AND coalesce(s.is_active, true) AND s.lifecycle_status = 'active' AND s.record_type = ").push_bind(value.to_owned()).push(")");
    }
    if let Some(q) = params.get("q").filter(|v| !v.is_empty()) {
        let q = q.trim();
        if q.chars().count() < 3 {
            return Err(());
        }
        builder.push(" AND strpos(lower(concat_ws(' ', p.preferred_full_name, p.preferred_nric, p.preferred_email, p.preferred_phone)), lower(").push_bind(q.to_owned()).push(")) > 0");
    }
    Ok(())
}

pub async fn list(
    State(state): State<AppState>,
    headers: HeaderMap,
    RawQuery(raw): RawQuery,
) -> Result<Json<ApiResponse<Vec<ListedPerson>>>, ApiError> {
    let id = request_id(&headers);
    let params = Params::parse(raw.as_deref(), LIST_PARAMS, &["entity_key", "source_key"])
        .map_err(|_| ApiError::invalid(id.clone()))?;
    let has_q = params.get("q").is_some_and(|value| !value.is_empty());
    let sort = params.get("sort_by").unwrap_or(if has_q {
        "relevance"
    } else {
        "profile_completeness_score"
    });
    let mut column = sort_column(sort).ok_or_else(|| ApiError::invalid(id.clone()))?;
    let sort = if column == "relevance" && !has_q {
        column = "p.profile_completeness_score";
        "profile_completeness_score"
    } else {
        sort
    };
    let direction = match params
        .get("sort_order")
        .map(str::to_ascii_uppercase)
        .as_deref()
    {
        None | Some("DESC") => " DESC",
        Some("ASC") => " ASC",
        _ => return Err(ApiError::invalid(id)),
    };
    let raw_limit = params
        .integer("limit")
        .map_err(|_| ApiError::invalid(id.clone()))?;
    let limit = raw_limit.filter(|value| *value > 0).unwrap_or(20).min(200);
    let offset = decode_offset(params.get("cursor")).map_err(|_| ApiError::invalid(id.clone()))?;
    let include_total = params
        .boolean("include_total")
        .map_err(|_| ApiError::invalid(id.clone()))?
        .unwrap_or(true);
    ready(&state, &id).await?;
    let mut tx = state
        .pool
        .begin()
        .await
        .map_err(|_| ApiError::database(id.clone()))?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        .execute(&mut *tx)
        .await
        .map_err(|_| ApiError::database(id.clone()))?;
    let total = if include_total {
        let mut count = QueryBuilder::<Postgres>::new("SELECT count(*) FROM person p");
        filters(&mut count, &params, sort, has_q).map_err(|_| ApiError::invalid(id.clone()))?;
        Some(
            count
                .build_query_scalar::<i64>()
                .fetch_one(&mut *tx)
                .await
                .map_err(|_| ApiError::database(id.clone()))?,
        )
    } else {
        None
    };
    let mut query = QueryBuilder::<Postgres>::new("SELECT p.list_payload FROM person p");
    filters(&mut query, &params, sort, has_q).map_err(|_| ApiError::invalid(id.clone()))?;
    query.push(" ORDER BY ");
    if column == "relevance" {
        let q = params
            .get("q")
            .ok_or_else(|| ApiError::invalid(id.clone()))?
            .trim();
        query.push("(");
        for (index, field) in [
            "preferred_full_name",
            "preferred_nric",
            "preferred_email",
            "preferred_phone",
        ]
        .iter()
        .enumerate()
        {
            if index > 0 {
                query.push(" + ");
            }
            query
                .push("CASE WHEN strpos(lower(coalesce(p.")
                .push(*field)
                .push(", '')), lower(")
                .push_bind(q.to_owned())
                .push(")) > 0 THEN 1 ELSE 0 END");
        }
        query.push(")");
    } else {
        query.push(column);
    }
    query
        .push(direction)
        .push(", p.person_id ASC OFFSET ")
        .push_bind(offset)
        .push(" LIMIT ")
        .push_bind(limit + 1);
    let rows = query
        .build()
        .fetch_all(&mut *tx)
        .await
        .map_err(|_| ApiError::database(id.clone()))?;
    let has_more = rows.len() > limit as usize;
    let mut persons: Vec<ListedPerson> = Vec::new();
    for row in rows.iter().take(limit as usize) {
        let value: Value = row
            .try_get("list_payload")
            .map_err(|_| ApiError::database(id.clone()))?;
        persons.push(serde_json::from_value(value).map_err(|_| ApiError::database(id.clone()))?);
    }
    tx.commit()
        .await
        .map_err(|_| ApiError::database(id.clone()))?;
    let next_cursor = if has_more {
        Some(encode_offset(
            offset
                .checked_add(limit)
                .ok_or_else(|| ApiError::invalid(id.clone()))?,
        ))
    } else {
        None
    };
    Ok(Json(ApiResponse::new(persons, id, next_cursor, total)))
}
