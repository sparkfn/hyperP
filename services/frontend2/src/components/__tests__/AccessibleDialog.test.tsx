// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
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

    const dialog = screen.getByRole("dialog", { name: "Edit user" });
    const content = screen.getByRole("button", { name: "Save" }).parentElement;

    expect(dialog.style.background).toBe("");
    expect(content?.style.padding).toBe("");
  });

  it("leaves the visual surface to its modal content", () => {
    render(
      <AccessibleDialog frameless open title="New OAuth Client" onClose={vi.fn()}>
        <div>OAuth client form</div>
      </AccessibleDialog>,
    );

    const dialog = screen.getByRole("dialog", { name: "New OAuth Client" });
    const content = screen.getByText("OAuth client form").parentElement;

    expect(dialog.style.background).toBe("transparent");
    expect(dialog.style.boxShadow).toBe("none");
    expect(content?.style.padding).toBe("0px");
    expect(content?.style.justifyContent).toBe("center");
  });

  it("closes from frameless gutters but not from modal content", () => {
    const onClose = vi.fn();
    render(
      <AccessibleDialog frameless open title="New OAuth Client" onClose={onClose}>
        <button type="button">Create Client</button>
      </AccessibleDialog>,
    );

    const button = screen.getByRole("button", { name: "Create Client" });
    const content = button.parentElement;
    if (content === null) {
      throw new Error("Dialog content was not rendered");
    }

    fireEvent.mouseDown(content);
    fireEvent.click(content);
    expect(onClose).toHaveBeenCalledOnce();

    onClose.mockClear();
    fireEvent.click(button);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("stays open when a drag from modal content ends in a frameless gutter", () => {
    const onClose = vi.fn();
    render(
      <AccessibleDialog frameless open title="Client Created" onClose={onClose}>
        <button type="button">Copy Client Secret</button>
      </AccessibleDialog>,
    );

    const button = screen.getByRole("button", { name: "Copy Client Secret" });
    const content = button.parentElement;
    if (content === null) {
      throw new Error("Dialog content was not rendered");
    }

    fireEvent.mouseDown(button);
    fireEvent.click(content);

    expect(onClose).not.toHaveBeenCalled();
  });
});
