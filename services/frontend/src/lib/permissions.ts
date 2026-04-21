// Role and authorization helpers shared between client and server.

export type Role = "admin" | "employee" | "first_time";

const ROLES: readonly Role[] = ["admin", "employee", "first_time"] as const;

export function isRole(value: unknown): value is Role {
  return typeof value === "string" && (ROLES as readonly string[]).includes(value);
}

export function isAdmin(role: Role | null | undefined): boolean {
  return role === "admin";
}

export function isActive(role: Role | null | undefined): boolean {
  return role === "admin" || role === "employee";
}

export function canMutateForEntity(
  role: Role | null | undefined,
  userEntityKey: string | null | undefined,
  targetEntityKey: string | null | undefined,
): boolean {
  if (role === "admin") return true;
  if (role !== "employee") return false;
  if (!userEntityKey || !targetEntityKey) return false;
  return userEntityKey === targetEntityKey;
}

export function canMutateAdminOnly(role: Role | null | undefined): boolean {
  return role === "admin";
}
