/**
 * Access-token TTL conversion + bounds for the OAuth admin UI.
 *
 * Mirrors the API model bounds (OAuthClient.access_token_ttl_seconds, 300–86400s).
 * Centralised so the create form and the manage panel clamp identically.
 */

const TTL_MIN_SECONDS = 300;
const TTL_MAX_SECONDS = 86400;
const TTL_DEFAULT_MINUTES = 15;

/** Parse a minutes input (form string or number) into a bounded TTL in seconds. */
export function ttlMinutesToSeconds(minutes: string | number): number {
  const parsed =
    typeof minutes === "number" ? minutes : parseInt(minutes || String(TTL_DEFAULT_MINUTES), 10);
  const safe = Number.isNaN(parsed) ? TTL_DEFAULT_MINUTES : parsed;
  return Math.max(TTL_MIN_SECONDS, Math.min(TTL_MAX_SECONDS, safe * 60));
}

/** Convert a stored TTL in seconds to whole minutes for display/editing. */
export function ttlSecondsToMinutes(seconds: number): number {
  return Math.round(seconds / 60);
}
