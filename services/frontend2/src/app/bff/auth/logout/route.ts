import "server-only";

import { NextResponse } from "next/server";

import { getToken } from "next-auth/jwt";
import { apiFetch } from "@/lib/api-server";

export async function POST(
  request: Request,
  _context: { params: Promise<Record<string, string>> },
): Promise<NextResponse> {
  const token = await getToken({ req: request });
  const idToken = (token?.googleIdToken as string | undefined) ?? "";
  const refreshToken = token?.googleRefreshToken as string | undefined;

  try {
    await apiFetch("/auth/logout", {
      method: "POST",
      authToken: idToken,
      body: refreshToken ? { refresh_token: refreshToken } : {},
    });
  } catch {
    // Revocation may fail if the token was already expired — still clear the session.
  }

  return NextResponse.json({ ok: true });
}
