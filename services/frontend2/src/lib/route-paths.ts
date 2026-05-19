export const BFF_AUTH_BASE_PATH = "/bff/auth";
export const API_HEALTH_PATH = "/api/health";
export const BFF_ME_PATH = "/bff/auth/me";
export const BFF_LOGOUT_PATH = "/bff/auth/logout";

export const PUBLIC_PATHS = ["/login", "/public/", API_HEALTH_PATH, BFF_AUTH_BASE_PATH] as const;

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}
