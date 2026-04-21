import "server-only";

import NextAuth, { type NextAuthConfig, type Session } from "next-auth";
import Google from "next-auth/providers/google";
import type { JWT } from "next-auth/jwt";

import type { Role } from "@/lib/permissions";

interface MeResponseBody {
  data: {
    email: string;
    google_sub: string;
    role: Role;
    entity_key: string | null;
    display_name: string | null;
  };
}

const API_BASE_URL: string = process.env.API_BASE_URL ?? "http://localhost:3000/v1";

async function fetchMe(idToken: string): Promise<MeResponseBody["data"] | null> {
  try {
    const res: Response = await fetch(`${API_BASE_URL}/auth/me`, {
      method: "GET",
      headers: {
        authorization: `Bearer ${idToken}`,
        accept: "application/json",
      },
      cache: "no-store",
    });
    if (!res.ok) return null;
    const json = (await res.json()) as MeResponseBody;
    return json.data;
  } catch {
    return null;
  }
}

export const authConfig: NextAuthConfig = {
  // Auth.js auto-reads AUTH_GOOGLE_ID / AUTH_GOOGLE_SECRET from env.
  providers: [Google],
  session: { strategy: "jwt", maxAge: 60 * 60 },
  pages: { signIn: "/login" },
  callbacks: {
    async jwt({ token, account, trigger }): Promise<JWT> {
      if (account?.id_token) {
        token.googleIdToken = account.id_token;
        const me = await fetchMe(account.id_token);
        if (me) {
          token.role = me.role;
          token.entityKey = me.entity_key;
          token.displayName = me.display_name;
        } else {
          token.role = "first_time";
          token.entityKey = null;
        }
      } else if (trigger === "update" && typeof token.googleIdToken === "string") {
        // Refresh role/entity_key from backend on explicit session.update().
        const me = await fetchMe(token.googleIdToken);
        if (me) {
          token.role = me.role;
          token.entityKey = me.entity_key;
          token.displayName = me.display_name;
        }
      }
      return token;
    },
    async session({ session, token }): Promise<Session> {
      if (session.user) {
        session.user.role = token.role ?? "first_time";
        session.user.entityKey = token.entityKey ?? null;
        session.user.displayName = token.displayName ?? null;
      }
      session.googleIdToken = token.googleIdToken;
      return session;
    },
    authorized({ auth: sess, request }): boolean | Response {
      const { pathname } = request.nextUrl;
      if (pathname.startsWith("/api/auth")) return true;
      if (pathname === "/login") return true;
      if (pathname === "/api/health") return true;
      if (!sess) return false;
      // First-time users may only reach the pending screen and the me endpoint.
      const role: string | undefined = sess.user?.role;
      if (role === "first_time") {
        if (pathname.startsWith("/pending")) return true;
        if (pathname === "/api/auth/me") return true;
        const url = request.nextUrl.clone();
        url.pathname = "/pending";
        return Response.redirect(url);
      }
      return true;
    },
  },
};

export const { handlers, auth, signIn, signOut } = NextAuth(authConfig);
