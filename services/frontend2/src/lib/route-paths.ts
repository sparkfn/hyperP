// Single source of truth for the web path this app is served under, read from
// the NEXT_PUBLIC_BASE_PATH build-time env (default "" = web root; set e.g.
// "/app/v2" to serve under a sub-path). next.config.ts reads the SAME var, so
// this one knob drives the Next.js basePath, next-auth's basePath (src/auth.ts),
// the middleware, and every client/server path helper below. (The nginx
// `location` block and the FastAPI mount are infra and must be kept in sync
// separately — see services/nginx/nginx.conf.)
//
// Under a non-empty basePath, middleware sees request paths that still carry the
// prefix, so route checks compare the app-relative path (toRelativePath) and
// redirects re-add the prefix (toBasePath). With BASE_PATH = "" both helpers are
// identities and the basePath-aware shims become no-ops.
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

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

export const PUBLIC_PATHS = ["/login", "/public/", API_HEALTH_PATH, BFF_AUTH_BASE_PATH] as const;

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}

// Routes only accessible by admin role.
const ADMIN_PATHS = ["/review", "/admin"] as const;

export function isAdminPath(pathname: string): boolean {
  return ADMIN_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}
