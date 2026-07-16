// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import AccessibleDialog from "../AccessibleDialog";

describe("AccessibleDialog", () => {
  it("exposes a labelled modal dialog", () => {
    render(
      <AccessibleDialog open title="Edit user" onClose={vi.fn()}>
        <button type="button">Save</button>
      </AccessibleDialog>,
    );

    expect(screen.getByRole("dialog", { name: "Edit user" })).toBeTruthy();
  });
});
