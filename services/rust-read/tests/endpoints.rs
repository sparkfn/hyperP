//! PostgreSQL-backed endpoint tests against a disposable projection database.
//!
//! The tests are skipped unless `RUST_READ_TEST_DATABASE_URL` is set. Seeding and
//! truncation additionally require `HYPERP_RUST_READ_TEST_DISPOSABLE=1`.

mod common;

use std::env;

use axum::{http::StatusCode, Router};
use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sqlx::{postgres::PgPoolOptions, PgPool};

use common::{app, call, REQUEST_ID, TOKEN};

const LIST_ITEM: &str = include_str!("fixtures/person_list_item.json");
const DETAIL: &str = include_str!("fixtures/person_detail.json");
const SUMMARY: &str = include_str!("fixtures/summary.json");
const EVENT_RESOLVED: &str = include_str!("fixtures/identity_event_resolved.json");
const EVENT_UNRESOLVED: &str = include_str!("fixtures/identity_event_unresolved.json");
const ERROR_NOT_FOUND: &str = include_str!("fixtures/error_not_found.json");

struct Fixtures {
    list_item: Value,
    second_item: Value,
    merged_item: Value,
    detail: Value,
    second_detail: Value,
    merged_detail: Value,
    summary: Value,
    resolved: Value,
    unresolved: Value,
    updated: Value,
}

fn fixtures() -> Fixtures {
    let list_item: Value = serde_json::from_str(LIST_ITEM).expect("list fixture");
    let detail: Value = serde_json::from_str(DETAIL).expect("detail fixture");
    let summary: Value = serde_json::from_str(SUMMARY).expect("summary fixture");
    let resolved: Value = serde_json::from_str(EVENT_RESOLVED).expect("resolved fixture");
    let unresolved: Value = serde_json::from_str(EVENT_UNRESOLVED).expect("unresolved fixture");
    let second_item = list_variant(&list_item, "p-0002", "active", 0.5, false, "Bob Lee");
    let merged_item = list_variant(&list_item, "p-0003", "merged", 0.9, true, "Carol Ng");
    let updated = event_variant(
        &resolved,
        "ev-3",
        3,
        "resolved",
        Some("p-0002"),
        "reviewed_activation",
        Some("ev-1"),
    );
    let second_detail = detail_variant(&detail, "p-0002", "active");
    let merged_detail = detail_variant(&detail, "p-0003", "merged");
    Fixtures {
        list_item,
        second_item,
        merged_item,
        detail,
        second_detail,
        merged_detail,
        summary,
        resolved,
        unresolved,
        updated,
    }
}

fn list_variant(
    base: &Value,
    person_id: &str,
    status: &str,
    completeness: f64,
    high_value: bool,
    name: &str,
) -> Value {
    let mut value = base.clone();
    value["person_id"] = json!(person_id);
    value["status"] = json!(status);
    value["profile_completeness_score"] = json!(completeness);
    value["is_high_value"] = json!(high_value);
    value["preferred_full_name"] = json!(name);
    let first = name
        .split_whitespace()
        .next()
        .expect("name has a first word");
    value["preferred_email"] = json!(format!("{}@example.com", first.to_lowercase()));
    value
}

fn detail_variant(base: &Value, person_id: &str, status: &str) -> Value {
    let mut value = base.clone();
    value["person_id"] = json!(person_id);
    value["status"] = json!(status);
    value
}

fn event_variant(
    base: &Value,
    event_id: &str,
    global_revision: i64,
    link_status: &str,
    person_id: Option<&str>,
    resolution_kind: &str,
    supersedes: Option<&str>,
) -> Value {
    let mut value = base.clone();
    value["event_id"] = json!(event_id);
    value["global_revision"] = json!(global_revision);
    value["link_status"] = json!(link_status);
    value["hyperp_person_id"] = person_id.map_or(Value::Null, |person| json!(person));
    value["resolution_kind"] = json!(resolution_kind);
    value["resolution_revision"] = json!(global_revision);
    value["supersedes_event_id"] = supersedes.map_or(Value::Null, |event| json!(event));
    value
}

async fn connect(url: &str) -> PgPool {
    PgPoolOptions::new()
        .max_connections(2)
        .connect(url)
        .await
        .expect("postgres connection")
}

async fn truncate(pool: &PgPool) {
    sqlx::query(
        "TRUNCATE TABLE person, person_identifier, person_address, person_connection, \
         source_record, identity_link_rev, summary_counter",
    )
    .execute(pool)
    .await
    .expect("truncate projection tables");
}

async fn insert_person(pool: &PgPool, payload: &Value, canonical_id: Option<&str>, detail: &Value) {
    let address_id = payload["preferred_address"]["address_id"].as_str();
    sqlx::query(
        "INSERT INTO person (person_id, canonical_id, status, preferred_full_name, \
         preferred_phone, preferred_email, preferred_dob, preferred_nric, preferred_address_id, \
         updated_at, profile_completeness_score, is_high_value, is_high_risk, source_record_count, \
         connection_count, entity_count, identifier_count, possible_match_count, \
         system_match_count, order_count, crm_deal_count, bankruptcy_case_count, phone_confidence, \
         list_payload, detail_payload) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, \
         $18, $19, $20, $21, $22, $23, $24, $25)",
    )
    .bind(payload["person_id"].as_str())
    .bind(canonical_id)
    .bind(payload["status"].as_str())
    .bind(payload["preferred_full_name"].as_str())
    .bind(payload["preferred_phone"].as_str())
    .bind(payload["preferred_email"].as_str())
    .bind(payload["preferred_dob"].as_str())
    .bind(payload["preferred_nric"].as_str())
    .bind(address_id)
    .bind(None::<DateTime<Utc>>)
    .bind(payload["profile_completeness_score"].as_f64())
    .bind(payload["is_high_value"].as_bool())
    .bind(payload["is_high_risk"].as_bool())
    .bind(payload["source_record_count"].as_i64())
    .bind(payload["connection_count"].as_i64())
    .bind(payload["entity_count"].as_i64())
    .bind(payload["identifier_count"].as_i64())
    .bind(payload["possible_match_count"].as_i64())
    .bind(payload["system_match_count"].as_i64())
    .bind(payload["order_count"].as_i64())
    .bind(payload["crm_deal_count"].as_i64())
    .bind(payload["bankruptcy_case_count"].as_i64())
    .bind(payload["phone_confidence"].as_f64())
    .bind(payload.clone())
    .bind(detail.clone())
    .execute(pool)
    .await
    .expect("insert person");
}

async fn insert_address(
    pool: &PgPool,
    person_id: &str,
    address_id: &str,
    street: &str,
    city: &str,
    postal: &str,
    country: &str,
) {
    sqlx::query(
        "INSERT INTO person_address (person_id, address_id, is_active, street_name, city, \
         postal_code, country_code) VALUES ($1, $2, true, $3, $4, $5, $6)",
    )
    .bind(person_id)
    .bind(address_id)
    .bind(street)
    .bind(city)
    .bind(postal)
    .bind(country)
    .execute(pool)
    .await
    .expect("insert address");
}

async fn insert_source_record(
    pool: &PgPool,
    pk: &str,
    person_id: &str,
    entity_key: &str,
    source_key: &str,
    record_type: &str,
) {
    sqlx::query(
        "INSERT INTO source_record (source_record_pk, person_id, entity_key, source_key, \
         record_type, lifecycle_status, is_active) VALUES ($1, $2, $3, $4, $5, 'active', true)",
    )
    .bind(pk)
    .bind(person_id)
    .bind(entity_key)
    .bind(source_key)
    .bind(record_type)
    .execute(pool)
    .await
    .expect("insert source record");
}

async fn insert_revision(
    pool: &PgPool,
    event_id: &str,
    global_revision: i64,
    link_key: &str,
    revision: &Value,
) {
    sqlx::query(
        "INSERT INTO identity_link_rev (event_id, global_revision, link_key, revision) \
         VALUES ($1, $2, $3, $4)",
    )
    .bind(event_id)
    .bind(global_revision)
    .bind(link_key)
    .bind(revision.clone())
    .execute(pool)
    .await
    .expect("insert identity revision");
}

async fn seed(pool: &PgPool, fixtures: &Fixtures) {
    truncate(pool).await;
    insert_person(pool, &fixtures.list_item, None, &fixtures.detail).await;
    insert_person(pool, &fixtures.second_item, None, &fixtures.second_detail).await;
    insert_person(
        pool,
        &fixtures.merged_item,
        Some("p-0001"),
        &fixtures.merged_detail,
    )
    .await;

    insert_address(
        pool,
        "p-0001",
        "a-1",
        "Orchard Road",
        "Singapore",
        "238801",
        "SG",
    )
    .await;
    insert_address(
        pool,
        "p-0002",
        "a-2",
        "Marine Parade",
        "Singapore",
        "440001",
        "SG",
    )
    .await;

    insert_source_record(pool, "sr-1", "p-0001", "eko", "eko_phppos", "identity").await;
    insert_source_record(
        pool,
        "sr-2",
        "p-0001",
        "speedzone",
        "speedzone_phppos",
        "sales",
    )
    .await;
    insert_source_record(pool, "sr-3", "p-0002", "eko", "eko_phppos", "identity").await;

    insert_revision(pool, "ev-1", 1, "ilk1:link-a", &fixtures.resolved).await;
    insert_revision(pool, "ev-2", 2, "ilk1:link-b", &fixtures.unresolved).await;
    insert_revision(pool, "ev-3", 3, "ilk1:link-a", &fixtures.updated).await;

    sqlx::query(
        "INSERT INTO summary_counter (id, high_water, last_success, current_revision, \
         identity_stream_ready, summary) VALUES ('global', now(), now(), 3, true, $1)",
    )
    .bind(fixtures.summary.clone())
    .execute(pool)
    .await
    .expect("mark projection ready");
}

fn rows(body: &Value) -> Vec<Value> {
    body["data"]
        .as_array()
        .expect("data must be an array")
        .clone()
}

fn assert_meta(body: &Value) {
    assert_eq!(body["meta"]["request_id"], REQUEST_ID);
    assert!(body["meta"].get("total_count").is_some());
}

async fn readiness(app: &Router, writer: &PgPool) {
    truncate(writer).await;
    sqlx::query("INSERT INTO summary_counter (id) VALUES ('global')")
        .execute(writer)
        .await
        .expect("reset summary counter");
    for uri in [
        "/v1/persons",
        "/v1/persons/p-0001",
        "/v1/persons/summary",
        "/v1/identity-links/events",
    ] {
        let (status, body) = call(app, uri, Some(TOKEN), REQUEST_ID).await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{uri}");
        assert!(body["error"]["code"].is_string(), "{uri}");
        assert!(body["error"]["details"].is_null(), "{uri}");
        assert_meta(&body);
    }
}

async fn all_five_endpoints(app: &Router, fixtures: &Fixtures) {
    let (status, body) = call(app, "/v1/persons", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        rows(&body),
        vec![fixtures.list_item.clone(), fixtures.second_item.clone()]
    );
    assert_eq!(body["meta"]["total_count"], json!(2));
    assert!(body["meta"]["next_cursor"].is_null());

    let (status, body) = call(app, "/v1/persons/p-0001", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["data"], fixtures.detail);

    let (status, body) = call(app, "/v1/persons/summary", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["data"], fixtures.summary);

    let (status, body) = call(
        app,
        "/v1/identity-links/events?limit=3",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        rows(&body),
        vec![
            fixtures.resolved.clone(),
            fixtures.unresolved.clone(),
            fixtures.updated.clone()
        ]
    );
    assert_eq!(body["through_revision"], json!(3));
    assert!(body.get("snapshot_revision").is_none());

    let (status, body) = call(
        app,
        "/v1/identity-links/snapshot?limit=10",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        rows(&body),
        vec![fixtures.updated.clone(), fixtures.unresolved.clone()]
    );
    assert_eq!(body["snapshot_revision"], json!(3));
    assert!(body.get("through_revision").is_none());
}

async fn list_scenarios(app: &Router, fixtures: &Fixtures) {
    let (status, first) = call(app, "/v1/persons?limit=1", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rows(&first), vec![fixtures.list_item.clone()]);
    assert_eq!(first["meta"]["total_count"], json!(2));
    let cursor = first["meta"]["next_cursor"]
        .as_str()
        .expect("first page cursor")
        .to_owned();

    let (status, second) = call(
        app,
        &format!("/v1/persons?limit=1&cursor={cursor}"),
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rows(&second), vec![fixtures.second_item.clone()]);
    assert!(second["meta"]["next_cursor"].is_null());

    let (status, body) = call(
        app,
        "/v1/persons?include_total=false",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["meta"]["total_count"].is_null());

    let (status, body) = call(
        app,
        "/v1/persons?sort_by=preferred_full_name&sort_order=asc",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        rows(&body),
        vec![fixtures.list_item.clone(), fixtures.second_item.clone()]
    );

    for (uri, expected) in [
        (
            "/v1/persons?is_high_value=true",
            vec![fixtures.list_item.clone()],
        ),
        (
            "/v1/persons?addr_street=orchard",
            vec![fixtures.list_item.clone()],
        ),
        (
            "/v1/persons?entity_key=eko",
            vec![fixtures.list_item.clone(), fixtures.second_item.clone()],
        ),
        (
            "/v1/persons?entity_key=eko&entity_key=speedzone&entity_key_mode=and",
            vec![fixtures.list_item.clone()],
        ),
        (
            "/v1/persons?source_key=eko_phppos&source_key=speedzone_phppos&source_key_mode=and",
            vec![fixtures.list_item.clone()],
        ),
        ("/v1/persons?q=alice", vec![fixtures.list_item.clone()]),
    ] {
        let (status, body) = call(app, uri, Some(TOKEN), REQUEST_ID).await;
        assert_eq!(status, StatusCode::OK, "{uri}");
        assert_eq!(rows(&body), expected, "{uri}");
    }

    let (status, body) = call(app, "/v1/persons?q=al", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_request");
}

/// A person without a completeness score is left out of the default (score-sorted) list and
/// its total, like the FastAPI list, but a text search still finds it.
async fn null_score_scenarios(app: &Router, fixtures: &Fixtures, writer: &PgPool) {
    let unscored = list_variant(
        &fixtures.list_item,
        "p-0004",
        "active",
        0.0,
        false,
        "Dana Quill",
    );
    let unscored_detail = detail_variant(&fixtures.detail, "p-0004", "active");
    insert_person(writer, &unscored, None, &unscored_detail).await;
    sqlx::query("UPDATE person SET profile_completeness_score = NULL WHERE person_id = 'p-0004'")
        .execute(writer)
        .await
        .expect("clear completeness score");

    let (status, body) = call(app, "/v1/persons", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        rows(&body),
        vec![fixtures.list_item.clone(), fixtures.second_item.clone()]
    );
    assert_eq!(body["meta"]["total_count"], json!(2));

    let (status, body) = call(app, "/v1/persons?q=dana", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rows(&body), vec![unscored]);

    sqlx::query("DELETE FROM person WHERE person_id = 'p-0004'")
        .execute(writer)
        .await
        .expect("remove the unscored person");
}

async fn detail_scenarios(app: &Router, fixtures: &Fixtures) {
    // A merged person resolves one hop to the canonical payload, not its own row.
    let (status, body) = call(app, "/v1/persons/p-0003", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["data"], fixtures.detail);
    assert_ne!(body["data"], fixtures.merged_detail);

    let (status, body) = call(app, "/v1/persons/p-0002", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["data"], fixtures.second_detail);

    let (status, body) = call(app, "/v1/persons/p-9999", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let expected: Value = serde_json::from_str(ERROR_NOT_FOUND).expect("not-found fixture");
    assert_eq!(body, expected);
}

async fn snapshot_scenarios(app: &Router, fixtures: &Fixtures, writer: &PgPool) -> String {
    let (status, first) = call(
        app,
        "/v1/identity-links/snapshot?limit=1",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(first["snapshot_revision"], json!(3));
    assert_eq!(rows(&first), vec![fixtures.updated.clone()]);
    let cursor = first["meta"]["next_cursor"]
        .as_str()
        .expect("snapshot cursor")
        .to_owned();

    let (status, second) = call(
        app,
        &format!("/v1/identity-links/snapshot?limit=1&cursor={cursor}"),
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(second["snapshot_revision"], json!(3));
    assert_eq!(rows(&second), vec![fixtures.unresolved.clone()]);
    assert!(second["meta"]["next_cursor"].is_null());

    sqlx::query("UPDATE summary_counter SET identity_stream_ready = false WHERE id = 'global'")
        .execute(writer)
        .await
        .expect("pause snapshot readiness");
    let (status, body) = call(app, "/v1/identity-links/snapshot", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "identity_link_snapshot_not_ready");
    sqlx::query("UPDATE summary_counter SET identity_stream_ready = true WHERE id = 'global'")
        .execute(writer)
        .await
        .expect("restore snapshot readiness");

    cursor
}

async fn events_scenarios(
    app: &Router,
    fixtures: &Fixtures,
    writer: &PgPool,
    snapshot_cursor: &str,
) {
    let (status, first) = call(
        app,
        "/v1/identity-links/events?limit=2",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        rows(&first),
        vec![fixtures.resolved.clone(), fixtures.unresolved.clone()]
    );
    assert_eq!(first["through_revision"], json!(3));
    let cursor = first["meta"]["next_cursor"]
        .as_str()
        .expect("events cursor")
        .to_owned();

    let (status, second) = call(
        app,
        &format!("/v1/identity-links/events?limit=2&cursor={cursor}"),
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rows(&second), vec![fixtures.updated.clone()]);
    assert_eq!(second["through_revision"], json!(3));
    assert!(second["meta"]["next_cursor"].is_null());

    let (status, body) = call(
        app,
        "/v1/identity-links/events?after_revision=2",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(rows(&body), vec![fixtures.updated.clone()]);
    assert_eq!(body["through_revision"], json!(3));

    let (status, body) = call(
        app,
        &format!("/v1/identity-links/events?cursor={}", tamper(&cursor)),
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_cursor");

    // A cursor for the other stream kind is rejected by the handler.
    let (status, body) = call(
        app,
        &format!("/v1/identity-links/events?cursor={snapshot_cursor}"),
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_cursor");
    let (status, body) = call(
        app,
        &format!("/v1/identity-links/snapshot?cursor={cursor}"),
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "invalid_cursor");

    // A missing revision in the frozen window is a stream gap.
    sqlx::query("DELETE FROM identity_link_rev WHERE global_revision = 2")
        .execute(writer)
        .await
        .expect("open identity-link gap");
    let (status, body) = call(
        app,
        "/v1/identity-links/events?limit=50",
        Some(TOKEN),
        REQUEST_ID,
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "identity_link_revision_gap");
    insert_revision(writer, "ev-2", 2, "ilk1:link-b", &fixtures.unresolved).await;
}

fn tamper(token: &str) -> String {
    let mut bytes = token.as_bytes().to_vec();
    if let Some(last) = bytes.last_mut() {
        *last = if *last == b'A' { b'B' } else { b'A' };
    }
    String::from_utf8(bytes).expect("cursor is utf8")
}

async fn isolation(reader: &PgPool) {
    let user: String = sqlx::query_scalar("SELECT current_user")
        .fetch_one(reader)
        .await
        .expect("current_user");
    assert_eq!(user, "spike_reader");
    let read_only: String = sqlx::query_scalar("SELECT current_setting('transaction_read_only')")
        .fetch_one(reader)
        .await
        .expect("transaction_read_only");
    assert_eq!(read_only, "on");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM person")
        .fetch_one(reader)
        .await
        .expect("reader SELECT");
    assert!(count >= 1);
    for statement in [
        "INSERT INTO person (person_id, status, list_payload, detail_payload) \
         VALUES ('p-write', 'active', '{}'::jsonb, '{}'::jsonb)",
        "UPDATE person SET status = 'active' WHERE person_id = 'p-0001'",
        "DELETE FROM person WHERE person_id = 'p-0001'",
        "TRUNCATE TABLE person",
        "CREATE TABLE public.spike_write_probe (id int)",
    ] {
        assert!(
            sqlx::query(statement).execute(reader).await.is_err(),
            "reader must not write: {statement}"
        );
    }
}

#[tokio::test]
async fn postgres_projection_endpoints_match_the_fixtures() {
    let Ok(reader_url) = env::var("RUST_READ_TEST_DATABASE_URL") else {
        eprintln!("skipping PostgreSQL endpoint tests: RUST_READ_TEST_DATABASE_URL is not set");
        return;
    };
    let disposable = env::var("HYPERP_RUST_READ_TEST_DISPOSABLE").unwrap_or_default();
    assert_eq!(
        disposable, "1",
        "refusing to seed a shared database: set HYPERP_RUST_READ_TEST_DISPOSABLE=1"
    );
    let writer_url = env::var("RUST_READ_TEST_WRITER_URL")
        .expect("RUST_READ_TEST_WRITER_URL is required for the disposable endpoint tests");

    let writer = connect(&writer_url).await;
    let reader = connect(&reader_url).await;
    let app = app(reader.clone());
    let fixtures = fixtures();

    readiness(&app, &writer).await;
    seed(&writer, &fixtures).await;
    all_five_endpoints(&app, &fixtures).await;
    list_scenarios(&app, &fixtures).await;
    detail_scenarios(&app, &fixtures).await;
    let snapshot_cursor = snapshot_scenarios(&app, &fixtures, &writer).await;
    events_scenarios(&app, &fixtures, &writer, &snapshot_cursor).await;
    null_score_scenarios(&app, &fixtures, &writer).await;
    isolation(&reader).await;
}
