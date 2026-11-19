use hyperp_rust_read::{router, AppState};
use sqlx::postgres::PgPoolOptions;
use std::env;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let url = env::var("DATABASE_URL")?;
    let token = env::var("RUST_SPIKE_BEARER_TOKEN")?;
    let secret = env::var("RUST_SPIKE_CURSOR_SECRET")?;
    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&url)
        .await
        .map_err(|_| "projection database connection failed")?;
    let restricted = sqlx::query_scalar::<_, bool>(
        "SELECT current_user = 'spike_reader' AND current_setting('transaction_read_only') = 'on'",
    )
    .fetch_one(&pool)
    .await
    .map_err(|_| "projection database role check failed")?;
    if !restricted {
        return Err("projection database must use the read-only spike_reader role".into());
    }
    let state = AppState::new(pool, &token, &secret)?;
    let listener = tokio::net::TcpListener::bind("0.0.0.0:3002").await?;
    axum::serve(listener, router(state)).await?;
    Ok(())
}
