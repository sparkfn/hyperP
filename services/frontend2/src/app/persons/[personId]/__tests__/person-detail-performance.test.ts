import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const pageSource = readFileSync(new URL("../page.tsx", import.meta.url), "utf8");

describe("Person detail performance boundaries", () => {
  it("bounds the initial request pair and aborts it on navigation", () => {
    expect(pageSource).toContain("const controller = new AbortController();");
    expect(pageSource).toContain("identifiers?limit=50");
    expect(pageSource).not.toContain("identifiers?limit=200");
    expect(pageSource).toContain("controller.abort();");
    expect(pageSource).toContain("identifierLoadControllerRef.current?.abort();");
  });

  it("does not eagerly request secondary detail resources from the page owner", () => {
    const effectStart = pageSource.indexOf("async function loadPersonDetail");
    const effectEnd = pageSource.indexOf("const [shareError", effectStart);
    const initialEffect = pageSource.slice(effectStart, effectEnd);

    for (const path of [
      "/source-records?limit=20",
      "/sales?limit=20",
      "/audit?limit=20",
      "/bankruptcy-cases?limit=20",
      "/source-record-entities",
      "/loyalty",
      "/vehicles",
    ]) {
      expect(initialEffect).not.toContain(path);
    }
  });

  it("defers section mounting until the section enters the near viewport", () => {
    expect(pageSource).toContain("const [activated, setActivated] = useState(!lazy);");
    expect(pageSource).toContain('{ rootMargin: "320px 0px" }');
    expect(pageSource).toContain('lazy={section.id !== "section-identifiers"}');
  });

  it("keeps supplemental sidebar and source-facet data off the critical path with replacement loaders", () => {
    expect(pageSource).toContain("function SidebarSupplementalCards");
    expect(pageSource).toContain("/bankruptcy-cases?limit=50");
    expect(pageSource).toContain("/loyalty");
    expect(pageSource).toContain("/vehicles");
    expect(pageSource).toContain("/source-record-entities");
  });

  it("uses one graph owner by opening only the dialog graph", () => {
    expect(pageSource).toContain("<PersonGraphDialog");
    expect(pageSource).not.toContain("PersonFocusedGraph");
    expect(pageSource).not.toContain("graphEnabled");
    expect(pageSource).toContain("Open graph");
  });

  it("restores abortable, bounded timeline data outside the initial load effect", () => {
    expect(pageSource).toContain("function TimelineTab");
    expect(pageSource).toContain("const timelineRef = useRef<HTMLElement | null>(null);");
    expect(pageSource).toContain("/source-records?limit=20");
    expect(pageSource).toContain("/sales?limit=20");
    expect(pageSource).toContain("/audit?limit=20");
    expect(pageSource).toContain("Promise.allSettled([");
    expect(pageSource).toContain("return () => controller.abort();");

    const effectStart = pageSource.indexOf("async function loadPersonDetail");
    const effectEnd = pageSource.indexOf("const [shareError", effectStart);
    const initialEffect = pageSource.slice(effectStart, effectEnd);
    expect(initialEffect).not.toContain("/source-records?limit=20");
    expect(initialEffect).not.toContain("/sales?limit=20");
    expect(initialEffect).not.toContain("/audit?limit=20");
  });

  it("provides a cursor-backed load-more path for identifiers beyond the first page", () => {
    expect(pageSource).toContain("identifiersNextCursor");
    expect(pageSource).toContain("loadMoreIdentifiers");
    expect(pageSource).toContain("Load more identifiers");
  });

  it("does not issue a second unresolved review-case request on the default tab", () => {
    expect(pageSource).toContain("if (!showResolvedReviewCases) return;");
    expect(pageSource).toContain("setOpenReviewDecisionIds(");
  });
});
