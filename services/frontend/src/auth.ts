import "server-only";

import NextAuth, {
  type NextAuthConfig,
  type Session,
} from "next-auth";
import Google from "next-auth/providers/google";
import type { JWT } from "next-auth/jwt";

import { BFF_AUTH_BASE_PATH, BFF_ME_PATH, toBasePath, toRelativePath } from "@/lib/route-paths";
import { buildApiUrl } from "@/lib/api-url";
import type { Role } from "@/lib/permissions";

declare module "next-auth" {
  interface Session {
    googleIdToken?: string;
    error?: string;
    user: {
      role: Role;
      entityKey: string | null;
      displayName: string | null;
    } & {
      id?: string;
      name?: string | null;
      email?: string | null;
      image?: string | null;
    };
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    googleIdToken?: string;
    googleRefreshToken?: string;
    googleIdTokenExpiresAt?: number;
  }
}

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

type GoogleRefreshOutcome =
  | { idToken: string; expiresAt: number }
  | "rejected"
  | "transient";

async function refreshGoogleIdToken(
  refreshToken: string,
): Promise<GoogleRefreshOutcome> {
  const clientId = process.env.AUTH_GOOGLE_ID;
  const clientSecret = process.env.AUTH_GOOGLE_SECRET;
  if (!clientId || !clientSecret) return "rejected";
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
    if (!res.ok) {
      // 4xx (e.g. invalid_grant) means the refresh token is dead — sign out.
      // 5xx is a Google-side hiccup — keep the session and retry next request.
      return res.status < 500 ? "rejected" : "transient";
    }
    const data = (await res.json()) as GoogleRefreshResponse;
    return {
      idToken: data.id_token,
      expiresAt: Math.floor(Date.now() / 1000) + data.expires_in,
    };
  } catch {
    return "transient";
  }
}

// Rolling idle window for the NextAuth session cookie, in seconds. The cookie
// expiry is re-issued on each request, so this is an inactivity timeout, not an
// absolute cap. Defaults to 1 hour when the env var is unset or invalid.
function sessionMaxAgeSeconds(): number {
  const raw = process.env.AUTH_SESSION_MAX_AGE_SECONDS;
  const parsed = raw === undefined ? NaN : Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 60 * 60;
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
  // Absolute auth-route path including the Next.js basePath ("/app/v1"). next-auth
  // uses this verbatim and does not auto-prepend the framework basePath, so the
  // value must match where the handler is actually served: /app/v1/bff/auth.
  basePath: `/app/v1${BFF_AUTH_BASE_PATH}`,
  providers: [
    Google({
      // access_type=offline makes Google issue a refresh_token, which the jwt
      // callback uses to silently renew the ID token past its 1 h lifetime.
      // prompt=consent is required because tokens live only in the JWT cookie
      // (no DB): Google returns a refresh_token solely on consent, so every
      // sign-in must re-consent or a re-login would leave the session
      // refresh-less and back to hourly expiry.
      authorization: {
        params: { access_type: "offline", prompt: "consent" },
      },
    }),
  ],
  session: { strategy: "jwt", maxAge: sessionMaxAgeSeconds() },
  pages: { signIn: "/login" },
  cookies: {
    sessionToken: {
      name: "hyperP_refresh",
      options: {
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        secure: process.env.NODE_ENV === "production",
      },
    },
  },
  callbacks: {
    async jwt({ token, account, trigger }): Promise<JWT> {
      if (account?.id_token) {
        token.googleIdToken = account.id_token;
        token.googleRefreshToken = account.refresh_token ?? undefined;
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

      // Refresh the access token if it is within 60 s of expiry.
      const expiresAt = token.googleIdTokenExpiresAt;
      const refreshToken = token.googleRefreshToken;
      if (
        typeof expiresAt === "number" &&
        typeof refreshToken === "string" &&
        Date.now() / 1000 > expiresAt - 60
      ) {
        const refreshed = await refreshGoogleIdToken(refreshToken);
        if (typeof refreshed === "object") {
          token.googleIdToken = refreshed.idToken;
          token.googleIdTokenExpiresAt = refreshed.expiresAt;
        } else if (refreshed === "rejected") {
          // Refresh token is expired or revoked — signal NextAuth to sign out.
          token.error = "RefreshTokenError";
        }
        // "transient": keep the current token; the next request retries.
      }

      if (trigger === "update" && typeof token.googleIdToken === "string") {
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
      session.error = typeof token.error === "string" ? token.error : undefined;
      return session;
    },
    authorized({ auth: sess, request }): boolean | Response {
      const pathname = toRelativePath(request.nextUrl.pathname);
      if (pathname.startsWith(BFF_AUTH_BASE_PATH)) return true;
      if (pathname === "/login") return true;
      if (pathname === "/api/health") return true;
      if (pathname.startsWith("/public/")) return true;
      if (!sess || !sess.googleIdToken) {
        // Redirect to the basePath-correct login rather than returning false,
        // which would route to pages.signIn without the basePath and loop.
        const url = request.nextUrl.clone();
        url.pathname = toBasePath("/login");
        url.search = "";
        return Response.redirect(url);
      }
      const role: string | undefined = sess.user?.role;
      if (role === "first_time") {
        if (pathname.startsWith("/pending")) return true;
        if (pathname === BFF_ME_PATH) return true;
        const url = request.nextUrl.clone();
        url.pathname = toBasePath("/pending");
        return Response.redirect(url);
      }
      return true;
    },
  },
};

export const { handlers, auth, signIn, signOut } = NextAuth(authConfig);
