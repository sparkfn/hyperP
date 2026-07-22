import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const moduleCss = readFileSync(
  new URL("../ProfileAnalysis.module.css", import.meta.url),
  "utf8",
);
const globalCss = readFileSync(new URL("../../app/globals.css", import.meta.url), "utf8");

describe("Profile analysis theme colors", () => {
  it("uses theme-aware semantic status token pairs", () => {
    expect(moduleCss).toContain("background: var(--warn-bg);");
    expect(moduleCss).toContain("color: var(--warn-text);");
    expect(moduleCss).toContain("background: var(--bad-soft);");
    expect(moduleCss).toContain("color: var(--bad);");
    expect(moduleCss).toContain("background: var(--good-soft);");
    expect(moduleCss).toContain("color: var(--good);");
    expect(moduleCss).not.toMatch(/var\(--(?:warning|danger|success)/);
  });

  it("defines every status token in the dark theme", () => {
    const darkTheme = globalCss.slice(globalCss.indexOf(':root[data-theme="dark"]'));

    const statusTokens = [
      "--warn-bg",
      "--warn-text",
      "--bad-soft",
      "--bad",
      "--good-soft",
      "--good",
    ];
    for (const token of statusTokens) {
      expect(darkTheme).toContain(`${token}:`);
    }
  });
});
