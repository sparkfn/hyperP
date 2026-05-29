import "server-only";

const API_BASE_URL: string = process.env.API_BASE_URL ?? "http://localhost:3000";

export function buildApiUrl(path: string, versioned: boolean = true): URL {
  const base: string = API_BASE_URL.replace(/\/+$/, "");
  const normalized: string = path.startsWith("/") ? path : `/${path}`;
  if (!versioned) {
    return new URL(`${base}${normalized}`);
  }
  // Public (unauthenticated) endpoints live on the main app at /v1/public, not on
  // the auth-gated /app/v1 frontend mount, so route them to /v1 instead.
  if (normalized.startsWith("/public/")) {
    return new URL(`${base}/v1${normalized}`);
  }
  const upstreamPath: string =
    !normalized.startsWith("/v1/") && !normalized.startsWith("/app/v1/")
      ? `/app/v1${normalized}`
      : normalized;
  return new URL(`${base}${upstreamPath}`);
}
