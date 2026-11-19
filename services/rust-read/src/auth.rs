use axum::{
    body::Body,
    extract::State,
    http::{header::AUTHORIZATION, HeaderValue, Request, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

use crate::{
    error::{request_id, ApiError},
    AppState,
};

pub async fn authorize(
    State(state): State<AppState>,
    mut request: Request<Body>,
    next: Next,
) -> Response {
    let id = request_id(request.headers());
    let candidate = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "));
    let authorized = candidate
        .map(|token| {
            let digest = Sha256::digest(token.as_bytes());
            bool::from(digest.as_slice().ct_eq(&state.bearer_hash))
        })
        .unwrap_or(false);
    if !authorized {
        return ApiError::new(
            StatusCode::UNAUTHORIZED,
            "unauthorized",
            "Invalid bearer token.",
            id,
        )
        .into_response();
    }
    // The generated or supplied ID is reused by handlers and errors.
    if let Ok(value) = HeaderValue::from_str(&id) {
        request.headers_mut().insert("x-request-id", value);
    }
    next.run(request).await
}
