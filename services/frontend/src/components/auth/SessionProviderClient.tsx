"use client";

import type { ReactElement, ReactNode } from "react";
import { SessionProvider } from "next-auth/react";

interface SessionProviderClientProps {
  children: ReactNode;
}

export default function SessionProviderClient(
  props: SessionProviderClientProps,
): ReactElement {
  return <SessionProvider>{props.children}</SessionProvider>;
}
