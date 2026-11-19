use axum::{
    extract::{RawQuery, State},
    http::{HeaderMap, StatusCode},
    Json,
};
use serde_json::Value;
use sqlx::{postgres::PgRow, Row};

use crate::{
    contracts::{IdentityLinkRevision, IdentityPage, ResponseMeta},
    cursors::{decode_identity, encode_identity, IdentityCursor},
    error::{request_id, ApiError},
    params::Params,
    AppState,
};

fn invalid_cursor(id: &str) -> ApiError {
    ApiError::new(
        StatusCode::BAD_REQUEST,
        "invalid_cursor",
        "Invalid identity-link cursor.",
        id.into(),
    )
}

fn gap(id: &str) -> ApiError {
    ApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "identity_link_revision_gap",
        "Identity-link stream has a revision gap.",
        id.into(),
    )
}

fn unavailable(id: &str) -> ApiError {
    ApiError::new(
        StatusCode::SERVICE_UNAVAILABLE,
        "identity_link_stream_unavailable",
        "Identity-link stream is unavailable.",
        id.into(),
    )
}

async fn stream_state(state: &AppState, id: &str) -> Result<(i64, bool), ApiError> {
    let row = sqlx::query("SELECT current_revision, identity_stream_ready FROM summary_counter WHERE id = 'global' AND last_success IS NOT NULL")
        .fetch_optional(&state.pool).await.map_err(|_| unavailable(id))?
        .ok_or_else(|| unavailable(id))?;
    let revision: i64 = row
        .try_get("current_revision")
        .map_err(|_| unavailable(id))?;
    let ready: bool = row
        .try_get("identity_stream_ready")
        .map_err(|_| unavailable(id))?;
    if revision < 0 {
        return Err(unavailable(id));
    }
    Ok((revision, ready))
}

fn limit(params: &Params, id: &str) -> Result<i64, ApiError> {
    match params
        .integer("limit")
        .map_err(|_| ApiError::invalid(id.into()))?
    {
        None => Ok(50),
        Some(value @ 1..=200) => Ok(value),
        _ => Err(ApiError::invalid(id.into())),
    }
}

fn decode_row(row: &PgRow, id: &str) -> Result<(IdentityLinkRevision, String), ApiError> {
    let event_id: String = row.try_get("event_id").map_err(|_| unavailable(id))?;
    let global_revision: i64 = row
        .try_get("global_revision")
        .map_err(|_| unavailable(id))?;
    let link_key: String = row.try_get("link_key").map_err(|_| unavailable(id))?;
    let value: Value = row.try_get("revision").map_err(|_| unavailable(id))?;
    let revision: IdentityLinkRevision =
        serde_json::from_value(value).map_err(|_| unavailable(id))?;
    let valid_status = matches!(
        revision.link_status.as_str(),
        "resolved" | "unresolved" | "pending_review" | "blocked" | "rejected" | "retired"
    );
    let valid_kind = matches!(
        revision.resolution_kind.as_str(),
        "baseline"
            | "automatic_activation"
            | "reviewed_activation"
            | "review_rejection"
            | "manual_no_match"
            | "source_supersession"
            | "person_merge"
            | "person_unmerge"
            | "person_retirement"
            | "source_retirement"
    );
    let valid_person = if revision.link_status == "resolved" {
        revision
            .hyperp_person_id
            .as_ref()
            .is_some_and(|v| !v.is_empty())
    } else {
        revision.hyperp_person_id.is_none()
    };
    if revision.event_id != event_id
        || revision.global_revision != global_revision
        || global_revision < 1
        || revision.resolution_revision < 1
        || link_key.is_empty()
        || !valid_status
        || !valid_kind
        || !matches!(
            revision.source_entity_type.as_str(),
            "deal" | "contact" | "lead" | "company"
        )
        || revision.source_system.is_empty()
        || revision.source_instance_id.is_empty()
        || revision.source_entity_id.is_empty()
        || revision.identity_policy_version.is_empty()
        || !valid_person
    {
        return Err(unavailable(id));
    }
    Ok((revision, link_key))
}

pub async fn events(
    State(state): State<AppState>,
    headers: HeaderMap,
    RawQuery(raw): RawQuery,
) -> Result<Json<IdentityPage<IdentityLinkRevision>>, ApiError> {
    let id = request_id(&headers);
    let params = Params::parse(raw.as_deref(), &["cursor", "after_revision", "limit"], &[])
        .map_err(|_| ApiError::invalid(id.clone()))?;
    let page_limit = limit(&params, &id)?;
    let after_param = params
        .integer("after_revision")
        .map_err(|_| ApiError::invalid(id.clone()))?;
    if after_param.is_some_and(|v| v < 0)
        || (params.get("cursor").is_some() && after_param.is_some())
    {
        return Err(ApiError::invalid(id));
    }
    let (current, _) = stream_state(&state, &id).await?;
    let (after, through) = if let Some(token) = params.get("cursor") {
        match decode_identity(token, &state.cursor_key).map_err(|_| invalid_cursor(&id))? {
            IdentityCursor::Events {
                after_revision,
                through_revision,
                ..
            } if through_revision <= current => (after_revision, through_revision),
            _ => return Err(invalid_cursor(&id)),
        }
    } else {
        (after_param.unwrap_or(0), current)
    };
    if after > through {
        return Err(gap(&id));
    }
    let rows = sqlx::query("SELECT event_id, global_revision, link_key, revision FROM identity_link_rev WHERE global_revision > $1 AND global_revision <= $2 ORDER BY global_revision LIMIT $3")
        .bind(after).bind(through).bind(page_limit).fetch_all(&state.pool).await.map_err(|_| unavailable(&id))?;
    let mut data = Vec::with_capacity(rows.len());
    let mut expected = after;
    for row in &rows {
        let (revision, _) = decode_row(row, &id)?;
        expected = expected.checked_add(1).ok_or_else(|| gap(&id))?;
        if revision.global_revision != expected {
            return Err(gap(&id));
        }
        data.push(revision);
    }
    if expected < through && (data.len() as i64) < page_limit {
        return Err(gap(&id));
    }
    let next_cursor = if expected < through {
        Some(
            encode_identity(
                &IdentityCursor::Events {
                    cursor_version: 1,
                    after_revision: expected,
                    through_revision: through,
                },
                &state.cursor_key,
            )
            .map_err(|_| unavailable(&id))?,
        )
    } else {
        None
    };
    Ok(Json(IdentityPage {
        data,
        meta: ResponseMeta {
            request_id: id,
            next_cursor,
            total_count: None,
        },
        through_revision: Some(through),
        snapshot_revision: None,
    }))
}

pub async fn snapshot(
    State(state): State<AppState>,
    headers: HeaderMap,
    RawQuery(raw): RawQuery,
) -> Result<Json<IdentityPage<IdentityLinkRevision>>, ApiError> {
    let id = request_id(&headers);
    let params = Params::parse(raw.as_deref(), &["cursor", "limit"], &[])
        .map_err(|_| ApiError::invalid(id.clone()))?;
    let page_limit = limit(&params, &id)?;
    let (current, ready) = stream_state(&state, &id).await?;
    if !ready {
        return Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "identity_link_snapshot_not_ready",
            "Identity-link baseline is not ready.",
            id,
        ));
    }
    let (bound, after_key) = if let Some(token) = params.get("cursor") {
        match decode_identity(token, &state.cursor_key).map_err(|_| invalid_cursor(&id))? {
            IdentityCursor::Snapshot {
                snapshot_revision,
                after_event_id,
                ..
            } if snapshot_revision <= current => {
                let key = sqlx::query_scalar::<_, String>("SELECT r.link_key FROM identity_link_rev r WHERE r.event_id = $1 AND r.global_revision = (SELECT max(r2.global_revision) FROM identity_link_rev r2 WHERE r2.link_key = r.link_key AND r2.global_revision <= $2)")
                    .bind(after_event_id).bind(snapshot_revision).fetch_optional(&state.pool).await.map_err(|_| unavailable(&id))?
                    .ok_or_else(|| invalid_cursor(&id))?;
                (snapshot_revision, key)
            }
            _ => return Err(invalid_cursor(&id)),
        }
    } else {
        (current, String::new())
    };
    let rows = sqlx::query("SELECT event_id, global_revision, link_key, revision FROM (SELECT DISTINCT ON (link_key) event_id, global_revision, link_key, revision FROM identity_link_rev WHERE global_revision <= $1 AND link_key > $2 ORDER BY link_key, global_revision DESC) AS latest ORDER BY link_key LIMIT $3")
        .bind(bound).bind(after_key).bind(page_limit + 1).fetch_all(&state.pool).await.map_err(|_| unavailable(&id))?;
    let has_more = rows.len() > page_limit as usize;
    let mut data = Vec::new();
    for row in rows.iter().take(page_limit as usize) {
        let (revision, _) = decode_row(row, &id)?;
        data.push(revision);
    }
    let next_cursor = if has_more {
        let after_event_id = data
            .last()
            .ok_or_else(|| unavailable(&id))?
            .event_id
            .clone();
        Some(
            encode_identity(
                &IdentityCursor::Snapshot {
                    cursor_version: 1,
                    snapshot_revision: bound,
                    after_event_id,
                },
                &state.cursor_key,
            )
            .map_err(|_| unavailable(&id))?,
        )
    } else {
        None
    };
    Ok(Json(IdentityPage {
        data,
        meta: ResponseMeta {
            request_id: id,
            next_cursor,
            total_count: None,
        },
        through_revision: None,
        snapshot_revision: Some(bound),
    }))
}
