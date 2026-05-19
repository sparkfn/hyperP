"use client";

import type { ReactElement, ReactNode } from "react";
import { SessionProvider } from "next-auth/react";

import { BFF_AUTH_BASE_PATH } from "@/lib/route-paths";

export default function SessionProviderClient({ children }: { children: ReactNode }): ReactElement {
  return <SessionProvider basePath={BFF_AUTH_BASE_PATH}>{children}</SessionProvider>;
}
