// @vitest-environment jsdom

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React, { type ReactElement, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import ActionToast, { type ToastState } from "../ActionToast";

interface ActionToastHarnessProps {
  onDismiss: () => void;
}

function ActionToastHarness({ onDismiss }: ActionToastHarnessProps): ReactElement | null {
  const [toast, setToast] = useState<ToastState | null>({
    type: "error",
    message: "Merge failed",
  });

  function handleDismiss(): void {
    onDismiss();
    setToast(null);
  }

  if (toast === null) {
    return null;
  }

  return <ActionToast type={toast.type} message={toast.message} onDismiss={handleDismiss} />;
}

describe("ActionToast", () => {
  it("announces severity and supports manual dismissal", async () => {
    const onDismiss = vi.fn();
    const user = userEvent.setup();

    render(<ActionToastHarness onDismiss={onDismiss} />);

    expect(screen.getByRole("alert").textContent).toContain("Merge failed");
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});
