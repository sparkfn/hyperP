"use client";

import type { ReactElement, ReactNode } from "react";
import type { Session } from "next-auth";
import { SessionProvider } from "next-auth/react";

import { BFF_AUTH_BASE_PATH, toBasePath } from "@/lib/route-paths";

interface SessionProviderClientProps {
  children: ReactNode;
  session: Session | null;
}

// The browser calls next-auth from the origin root, so the client basePath must
// include the app's BASE_PATH. Derived via toBasePath; at the web root this is
// simply /bff/auth.
export default function SessionProviderClient({ children, session }: SessionProviderClientProps): ReactElement {
  return (
    <SessionProvider
      basePath={toBasePath(BFF_AUTH_BASE_PATH)}
      refetchInterval={0}
      refetchOnWindowFocus={false}
      session={session}
    >
      {children}
    </SessionProvider>
  );
}
