//! Database-free contract tests: envelope shapes, request validation and cursors.

mod common;

use axum::{http::StatusCode, Router};
use serde::{de::DeserializeOwned, Serialize};
use serde_json::Value;
use sqlx::postgres::PgPoolOptions;

use common::{app, call, call_raw, REQUEST_ID, TOKEN};
use hyperp_rust_read::contracts::{
    ApiResponse, IdentityLinkRevision, IdentityPage, ListedPerson, Person, PersonListSummary,
};

const ERROR_UNAUTHORIZED: &str = include_str!("fixtures/error_unauthorized.json");
const ERROR_INVALID_REQUEST: &str = include_str!("fixtures/error_invalid_request.json");

fn lazy_router() -> Router {
    let pool = PgPoolOptions::new()
        .connect_lazy("postgresql://spike_reader:secret@127.0.0.1:5432/spike_projection")
        .expect("lazy pool");
    app(pool)
}

#[tokio::test]
async fn bearer_auth_uses_the_exact_error_envelope() {
    let app = lazy_router();
    let expected: Value = serde_json::from_str(ERROR_UNAUTHORIZED).expect("fixture");
    for token in [None, Some("wrong-token")] {
        let (status, body) = call(&app, "/v1/persons", token, REQUEST_ID).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(body, expected);
    }
    let (status, body) =
        call_raw(&app, "/v1/persons", Some("Basic dXNlcjpwYXNz"), REQUEST_ID).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(body, expected);
}

#[tokio::test]
async fn malformed_requests_are_rejected_before_database_access() {
    let app = lazy_router();
    let expected: Value = serde_json::from_str(ERROR_INVALID_REQUEST).expect("fixture");
    for uri in [
        "/v1/persons?bogus=1",
        "/v1/persons?sort_order=sideways",
        "/v1/persons?limit=abc",
        "/v1/persons?cursor=not-base64",
        "/v1/persons/summary?bogus=1",
        "/v1/persons/p-0001?bogus=1",
        "/v1/identity-links/events?limit=0",
        "/v1/identity-links/events?after_revision=-1",
    ] {
        let (status, body) = call(&app, uri, Some(TOKEN), REQUEST_ID).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{uri}");
        assert_eq!(body, expected, "{uri}");
    }
}

#[tokio::test]
async fn unknown_routes_return_the_standard_not_found_envelope() {
    let app = lazy_router();
    let expected = serde_json::json!({
        "error": {"code": "not_found", "message": "Not Found", "details": null},
        "meta": {
            "request_id": REQUEST_ID,
            "next_cursor": null,
            "total_count": null
        }
    });
    let (status, body) = call(&app, "/v1/nope", Some(TOKEN), REQUEST_ID).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body, expected);
}

#[test]
fn api_fixtures_round_trip_through_the_crate_contracts() {
    round_trip::<ListedPerson>(include_str!("fixtures/person_list_item.json"));
    round_trip::<Person>(include_str!("fixtures/person_detail.json"));
    round_trip::<PersonListSummary>(include_str!("fixtures/summary.json"));
    round_trip::<IdentityLinkRevision>(include_str!("fixtures/identity_event_resolved.json"));
    round_trip::<IdentityLinkRevision>(include_str!("fixtures/identity_event_unresolved.json"));
    round_trip::<ApiResponse<Vec<ListedPerson>>>(include_str!("fixtures/envelope_list.json"));
    round_trip::<IdentityPage<IdentityLinkRevision>>(include_str!("fixtures/envelope_events.json"));
    round_trip::<IdentityPage<IdentityLinkRevision>>(include_str!(
        "fixtures/envelope_snapshot.json"
    ));
}

fn round_trip<T: Serialize + DeserializeOwned>(raw: &str) {
    let typed: T = serde_json::from_str(raw).expect("fixture deserializes into the contract");
    let serialized = serde_json::to_value(&typed).expect("contract serializes");
    let expected: Value = serde_json::from_str(raw).expect("fixture is JSON");
    assert_eq!(serialized, expected);
}

#[test]
fn identity_cursors_are_authenticated_and_bounded() {
    use hyperp_rust_read::cursors::{
        decode_identity, decode_offset, encode_identity, encode_offset, IdentityCursor,
    };

    let events = IdentityCursor::Events {
        cursor_version: 1,
        after_revision: 2,
        through_revision: 5,
    };
    let token = encode_identity(&events, b"secret").expect("events cursor encodes");
    assert_eq!(decode_identity(&token, b"secret"), Ok(events));
    assert!(decode_identity(&token, b"wrong-secret").is_err());
    assert!(decode_identity(&token, b"").is_err());
    let mut tampered = token.into_bytes();
    tampered[0] = if tampered[0] == b'A' { b'B' } else { b'A' };
    assert!(decode_identity(std::str::from_utf8(&tampered).expect("ascii"), b"secret").is_err());

    let snapshot = IdentityCursor::Snapshot {
        cursor_version: 1,
        snapshot_revision: 3,
        after_event_id: "ev-2".into(),
    };
    let token = encode_identity(&snapshot, b"secret").expect("snapshot cursor encodes");
    assert_eq!(decode_identity(&token, b"secret"), Ok(snapshot));

    // Bounds are enforced by the same validation on encode and decode.
    for cursor in [
        IdentityCursor::Events {
            cursor_version: 1,
            after_revision: 5,
            through_revision: 5,
        },
        IdentityCursor::Events {
            cursor_version: 1,
            after_revision: -1,
            through_revision: 5,
        },
        IdentityCursor::Events {
            cursor_version: 2,
            after_revision: 1,
            through_revision: 5,
        },
        IdentityCursor::Snapshot {
            cursor_version: 1,
            snapshot_revision: 3,
            after_event_id: String::new(),
        },
    ] {
        assert!(encode_identity(&cursor, b"secret").is_err(), "{cursor:?}");
    }

    assert_eq!(decode_offset(None), Ok(0));
    assert_eq!(decode_offset(Some("")), Ok(0));
    assert_eq!(decode_offset(Some(&encode_offset(0))), Ok(0));
    assert_eq!(decode_offset(Some(&encode_offset(200))), Ok(200));
    assert!(decode_offset(Some("not-base64")).is_err());
    assert!(decode_offset(Some(&"A".repeat(33))).is_err());
    assert!(decode_offset(Some(&encode_offset(-1))).is_err());
}
