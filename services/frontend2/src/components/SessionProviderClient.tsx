"use client";

import type { ReactElement, ReactNode } from "react";
import { SessionProvider } from "next-auth/react";

import { BFF_AUTH_BASE_PATH, toBasePath } from "@/lib/route-paths";

// The browser calls next-auth from the origin root, so the client basePath must
// include the Next.js basePath: /app/v2/bff/auth.
export default function SessionProviderClient({ children }: { children: ReactNode }): ReactElement {
  return <SessionProvider basePath={toBasePath(BFF_AUTH_BASE_PATH)}>{children}</SessionProvider>;
}
