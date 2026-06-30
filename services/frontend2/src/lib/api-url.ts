import "server-only";

const API_BASE_URL: string = process.env.API_BASE_URL ?? "http://localhost:3000";

export function buildApiUrl(path: string, versioned: boolean = true): URL {
  const base: string = API_BASE_URL.replace(/\/+$/, "");
  const normalized: string = path.startsWith("/") ? path : `/${path}`;
  if (!versioned) {
    return new URL(`${base}${normalized}`);
  }
  // FastAPI mount this frontend's authenticated API contract is served under.
  // This is an API-side mount path and is independent of the frontend's web
  // BASE_PATH (the UI now serves at the web root) — do not couple the two.
  const API_MOUNT = "/app/v2";
  // Public (unauthenticated) endpoints live on the main app at /v1/public, not on
  // the auth-gated API mount, so route them to /v1 instead.
  if (normalized.startsWith("/public/")) {
    return new URL(`${base}/v1${normalized}`);
  }
  const upstreamPath: string = normalized.startsWith(`${API_MOUNT}/`) ? normalized : `${API_MOUNT}${normalized}`;
  return new URL(`${base}${upstreamPath}`);
}
