export const BFF_AUTH_BASE_PATH = "/bff/auth";
export const API_HEALTH_PATH = "/api/health";
export const BFF_ME_PATH = "/bff/auth/me";
export const BFF_LOGOUT_PATH = "/bff/auth/logout";

export const PUBLIC_PATHS = ["/login", "/public/", API_HEALTH_PATH, BFF_AUTH_BASE_PATH] as const;

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}

// Routes only accessible by admin role.
const ADMIN_PATHS = ["/review", "/admin"] as const;

export function isAdminPath(pathname: string): boolean {
  return ADMIN_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}
