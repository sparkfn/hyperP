import "server-only";

const API_BASE_URL: string = process.env.API_BASE_URL ?? "http://localhost:3000";

export function buildApiUrl(path: string, versioned: boolean = true): URL {
  const base: string = API_BASE_URL.replace(/\/+$/, "");
  const normalized: string = path.startsWith("/") ? path : `/${path}`;
  const upstreamPath: string = versioned && !normalized.startsWith("/v1/") ? `/v1${normalized}` : normalized;
  return new URL(`${base}${upstreamPath}`);
}
