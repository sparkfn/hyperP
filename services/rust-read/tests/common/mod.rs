//! Shared helpers for the integration test binaries.
#![allow(dead_code)]

use axum::{
    body::Body,
    http::{header, Request, StatusCode},
    Router,
};
use http_body_util::BodyExt;
use serde_json::Value;
use sqlx::PgPool;
use tower::ServiceExt;

pub const TOKEN: &str = "test-bearer";
pub const SECRET: &str = "test-cursor-secret";
pub const REQUEST_ID: &str = "req-test-0001";

pub fn app(pool: PgPool) -> Router {
    hyperp_rust_read::router(
        hyperp_rust_read::AppState::new(pool, TOKEN, SECRET).expect("app state"),
    )
}

pub async fn call(
    app: &Router,
    uri: &str,
    token: Option<&str>,
    request_id: &str,
) -> (StatusCode, Value) {
    let authorization = token.map(|token| format!("Bearer {token}"));
    call_raw(app, uri, authorization.as_deref(), request_id).await
}

pub async fn call_raw(
    app: &Router,
    uri: &str,
    authorization: Option<&str>,
    request_id: &str,
) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .uri(uri)
        .header("x-request-id", request_id);
    if let Some(value) = authorization {
        builder = builder.header(header::AUTHORIZATION, value);
    }
    let response = app
        .clone()
        .oneshot(builder.body(Body::empty()).expect("request"))
        .await
        .expect("infallible service");
    let status = response.status();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("response body")
        .to_bytes();
    (status, serde_json::from_slice(&bytes).expect("json body"))
}
