import type { DefaultSession } from "next-auth";
import type { Role } from "@/lib/permissions";

declare module "next-auth" {
  interface Session {
    user: {
      role: Role;
      entityKey: string | null;
      displayName: string | null;
    } & DefaultSession["user"];
    googleIdToken?: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    googleIdToken?: string;
    role?: Role;
    entityKey?: string | null;
    displayName?: string | null;
  }
}
