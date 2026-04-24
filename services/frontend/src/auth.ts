import "server-only";

import NextAuth, { type NextAuthConfig, type Session } from "next-auth";
import Google from "next-auth/providers/google";
import type { JWT } from "next-auth/jwt";

import { BFF_AUTH_BASE_PATH, BFF_ME_PATH } from "@/lib/route-paths";
import { buildApiUrl } from "@/lib/api-url";
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

interface GoogleRefreshResponse {
  id_token: string;
  access_token: string;
  expires_in: number;
}

async function refreshGoogleIdToken(
  refreshToken: string,
): Promise<{ idToken: string; expiresAt: number } | null> {
  const clientId = process.env.AUTH_GOOGLE_ID;
  const clientSecret = process.env.AUTH_GOOGLE_SECRET;
  if (!clientId || !clientSecret) return null;
  try {
    const res: Response = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: clientId,
        client_secret: clientSecret,
        grant_type: "refresh_token",
        refresh_token: refreshToken,
      }),
      cache: "no-store",
    });
    if (!res.ok) return null;
    const data = (await res.json()) as GoogleRefreshResponse;
    return {
      idToken: data.id_token,
      expiresAt: Math.floor(Date.now() / 1000) + data.expires_in,
    };
  } catch {
    return null;
  }
}

async function fetchMe(idToken: string): Promise<MeResponseBody["data"] | null> {
  try {
    const res: Response = await fetch(buildApiUrl("/auth/me"), {
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
  basePath: BFF_AUTH_BASE_PATH,
  // Auth.js auto-reads AUTH_GOOGLE_ID / AUTH_GOOGLE_SECRET from env.
  providers: [Google],
  session: { strategy: "jwt", maxAge: 60 * 60 },
  pages: { signIn: "/login" },
  callbacks: {
    async jwt({ token, account, trigger }): Promise<JWT> {
      if (account?.id_token) {
        token.googleIdToken = account.id_token;
        token.googleRefreshToken = account.refresh_token ?? undefined;
        // account.expires_at is the access-token expiry in seconds (also ~1h for Google ID tokens).
        token.googleIdTokenExpiresAt = account.expires_at ?? undefined;
        const me = await fetchMe(account.id_token);
        if (me) {
          token.role = me.role;
          token.entityKey = me.entity_key;
          token.displayName = me.display_name;
        } else {
          token.role = "first_time";
          token.entityKey = null;
        }
        return token;
      }

      // 60s early to avoid delivering an already-expired token on the next API call.
      const expiresAt = token.googleIdTokenExpiresAt;
      const refreshToken = token.googleRefreshToken;
      if (
        typeof expiresAt === "number" &&
        typeof refreshToken === "string" &&
        Date.now() / 1000 > expiresAt - 60
      ) {
        const refreshed = await refreshGoogleIdToken(refreshToken);
        if (refreshed) {
          token.googleIdToken = refreshed.idToken;
          token.googleIdTokenExpiresAt = refreshed.expiresAt;
        } else {
          // Refresh failed — clear tokens so the middleware forces re-login.
          token.googleIdToken = undefined;
        }
      }

      if (trigger === "update" && typeof token.googleIdToken === "string") {
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
      if (pathname.startsWith(BFF_AUTH_BASE_PATH)) return true;
      if (pathname === "/login") return true;
      if (pathname === "/api/health") return true;
      if (!sess || !sess.googleIdToken) return false;
      // First-time users may only reach the pending screen and the me endpoint.
      const role: string | undefined = sess.user?.role;
      if (role === "first_time") {
        if (pathname.startsWith("/pending")) return true;
        if (pathname === BFF_ME_PATH) return true;
        const url = request.nextUrl.clone();
        url.pathname = "/pending";
        return Response.redirect(url);
      }
      return true;
    },
  },
};

export const { handlers, auth, signIn, signOut } = NextAuth(authConfig);
