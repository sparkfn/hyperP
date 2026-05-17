import type { ReactElement } from "react";
import Box from "@mui/material/Box";

import PersonFocusedGraph from "@/components/PersonFocusedGraph";

interface GraphPageProps {
  searchParams: Promise<{ element_id?: string; name?: string }>;
}

export default async function GraphPage({ searchParams }: GraphPageProps): Promise<ReactElement> {
  const { element_id, name } = await searchParams;
  const title = name ?? element_id ?? "Graph Explorer";

  return (
    <Box sx={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <PersonFocusedGraph initialElementId={element_id} initialTitle={title} />
    </Box>
  );
}
