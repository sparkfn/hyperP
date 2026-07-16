import { describe, expect, it, vi } from "vitest";

import { createSingleFlight } from "../single-flight";

describe("createSingleFlight", () => {
  it("shares one in-flight operation for the same key", async () => {
    const operation = vi.fn(async (key: string): Promise<string> => {
      await Promise.resolve();
      return `${key}-result`;
    });
    const run = createSingleFlight(operation);

    const [first, second] = await Promise.all([run("session"), run("session")]);

    expect(first).toBe("session-result");
    expect(second).toBe("session-result");
    expect(operation).toHaveBeenCalledOnce();
  });

  it("starts a new operation after the first settles", async () => {
    const operation = vi.fn(async (key: string): Promise<string> => key);
    const run = createSingleFlight(operation);

    await run("session");
    await run("session");

    expect(operation).toHaveBeenCalledTimes(2);
  });
});
