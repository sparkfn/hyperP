import type { ReactElement } from "react";

import GraphExplorer from "@/components/GraphExplorer";

interface GraphPageProps {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}

export default async function GraphPage({ searchParams }: GraphPageProps): Promise<ReactElement> {
  const params = await searchParams;
  const personId = typeof params["person_id"] === "string" ? params["person_id"] : undefined;
  const elementId = typeof params["element_id"] === "string" ? params["element_id"] : undefined;
  const name = typeof params["name"] === "string" ? params["name"] : undefined;
  const label = typeof params["label"] === "string" ? params["label"] : undefined;
  return (
    <GraphExplorer
      initialPersonId={personId}
      initialElementId={elementId}
      initialName={name}
      initialLabel={label}
    />
  );
}
