use axum::{
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;
use uuid::Uuid;

#[derive(Debug)]
pub struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
    request_id: String,
}

impl ApiError {
    pub fn new(
        status: StatusCode,
        code: &'static str,
        message: &'static str,
        request_id: String,
    ) -> Self {
        Self {
            status,
            code,
            message,
            request_id,
        }
    }

    pub fn invalid(request_id: String) -> Self {
        Self::new(
            StatusCode::BAD_REQUEST,
            "invalid_request",
            "Invalid request.",
            request_id,
        )
    }

    pub fn database(request_id: String) -> Self {
        Self::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "projection_unavailable",
            "Projection is unavailable.",
            request_id,
        )
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({
                "error": {"code": self.code, "message": self.message, "details": null},
                "meta": {"request_id": self.request_id, "next_cursor": null, "total_count": null}
            })),
        )
            .into_response()
    }
}

pub fn request_id(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|v| v.to_str().ok())
        .filter(|v| !v.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().to_string())
}

pub async fn not_found(headers: HeaderMap) -> ApiError {
    ApiError::new(
        StatusCode::NOT_FOUND,
        "not_found",
        "Not Found",
        request_id(&headers),
    )
}
