// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import ActionToast from "../ActionToast";

describe("ActionToast", () => {
  it("announces severity and supports manual dismissal", () => {
    const onDismiss = vi.fn();

    render(<ActionToast type="error" message="Merge failed" onDismiss={onDismiss} />);

    expect(screen.getByRole("alert").textContent).toContain("Merge failed");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});
