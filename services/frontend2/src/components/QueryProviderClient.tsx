"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactElement, type ReactNode } from "react";

import { createHyperPQueryClient } from "@/lib/query-client";

export default function QueryProviderClient({ children }: { children: ReactNode }): ReactElement {
  const [client] = useState(createHyperPQueryClient);

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
