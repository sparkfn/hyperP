// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { CrmDealCount } from "../CrmDealCount";

afterEach(cleanup);

describe("CrmDealCount", () => {
  it("renders the muted no-deals state for zero", () => {
    render(<CrmDealCount count={0} />);

    expect(screen.getByText("No deals").className).toContain("crmDealCountZero");
  });

  it("renders positive counts with the compact count and label treatment", () => {
    render(<CrmDealCount count={12} />);

    expect(screen.getByText("12").className).toContain("crmDealCount");
    expect(screen.getByText("deals").className).toContain("crmDealLabel");
  });
});
