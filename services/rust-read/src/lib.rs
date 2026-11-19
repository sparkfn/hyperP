//! Isolated, read-only PostgreSQL projection reader for the Rust spike.

pub mod auth;
pub mod contracts;
pub mod cursors;
pub mod error;
pub mod identity_links;
mod params;
pub mod persons;

use axum::{middleware, routing::get, Router};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use std::sync::Arc;

#[derive(Clone)]
pub struct AppState {
    pub pool: PgPool,
    pub(crate) bearer_hash: [u8; 32],
    pub(crate) cursor_key: Arc<[u8]>,
}

impl AppState {
    pub fn new(pool: PgPool, token: &str, cursor_secret: &str) -> Result<Self, &'static str> {
        if token.trim().is_empty() || cursor_secret.trim().is_empty() {
            return Err("spike bearer token and cursor secret are required");
        }
        Ok(Self {
            pool,
            bearer_hash: Sha256::digest(token.as_bytes()).into(),
            cursor_key: Arc::from(cursor_secret.as_bytes()),
        })
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/v1/persons/summary", get(persons::summary))
        .route("/v1/persons", get(persons::list))
        .route("/v1/persons/{id}", get(persons::detail))
        .route("/v1/identity-links/events", get(identity_links::events))
        .route("/v1/identity-links/snapshot", get(identity_links::snapshot))
        .fallback(error::not_found)
        .layer(middleware::from_fn_with_state(
            state.clone(),
            auth::authorize,
        ))
        .with_state(state)
}
