import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const pageSource = readFileSync(new URL("../page.tsx", import.meta.url), "utf8");

describe("Person detail profile analysis integration", () => {
  it("places Profile analysis first in the loaded and skeleton navigation", () => {
    const labelsStart = pageSource.indexOf("const sectionLabels = [");
    const profileLabel = pageSource.indexOf('"Profile analysis"', labelsStart);
    const matchesLabel = pageSource.indexOf('"Matches"', labelsStart);
    expect(profileLabel).toBeGreaterThan(labelsStart);
    expect(profileLabel).toBeLessThan(matchesLabel);
    expect(pageSource.indexOf('{/* Profile analysis — full width */}')).toBeGreaterThan(-1);
    expect(pageSource.indexOf('{/* Profile analysis — full width */}')).toBeLessThan(
      pageSource.indexOf('{/* Matches — full width */}'),
    );
  });

  it("loads profile analysis with the canonical Person returned by the API", () => {
    expect(pageSource).toContain("<ProfileAnalysisPanel personId={person.person_id} />");
    expect(pageSource).not.toContain("<ProfileAnalysisPanel personId={personId} />");
  });
});
