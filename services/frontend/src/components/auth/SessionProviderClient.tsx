"use client";

import type { ReactElement, ReactNode } from "react";
import { SessionProvider } from "next-auth/react";

import { BFF_AUTH_BASE_PATH } from "@/lib/route-paths";

interface SessionProviderClientProps {
  children: ReactNode;
}

export default function SessionProviderClient(
  props: SessionProviderClientProps,
): ReactElement {
  return <SessionProvider basePath={BFF_AUTH_BASE_PATH}>{props.children}</SessionProvider>;
}
