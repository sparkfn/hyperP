// Next.js basePath this app is served under. Under a basePath, middleware sees
// request paths that still carry the prefix, so route checks must compare the
// app-relative path (see toRelativePath) and redirects must re-add the prefix.
export const BASE_PATH = "/app/v1";

export const BFF_AUTH_BASE_PATH = "/bff/auth";
export const API_HEALTH_PATH = "/api/health";
export const BFF_ME_PATH = "/bff/auth/me";
export const BFF_LOGOUT_PATH = "/bff/auth/logout";

/** Strip the basePath prefix so route checks compare app-relative paths. */
export function toRelativePath(pathname: string): string {
  if (pathname === BASE_PATH) return "/";
  return pathname.startsWith(`${BASE_PATH}/`) ? pathname.slice(BASE_PATH.length) : pathname;
}

/** Re-add the basePath prefix when building a redirect target. */
export function toBasePath(relativePath: string): string {
  return `${BASE_PATH}${relativePath}`;
}
