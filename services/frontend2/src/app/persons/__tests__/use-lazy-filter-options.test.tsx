// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { bffFetch } = vi.hoisted(() => ({ bffFetch: vi.fn() }));

vi.mock("@/lib/api-client", () => ({ bffFetch }));

import { useLazyFilterOptions } from "../use-lazy-filter-options";

interface Option {
  key: string;
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  bffFetch.mockReset();
});

describe("useLazyFilterOptions", () => {
  it("does not request options before being enabled and started", () => {
    const { result } = renderHook(() => useLazyFilterOptions<Option>({
      enabled: false,
      initialEnabled: false,
      path: "/bff/options",
    }));

    expect(result.current.status).toBe("idle");
    expect(bffFetch).not.toHaveBeenCalled();
  });

  it("aborts on close and requests again when reopened", async () => {
    const first = deferred<Option[]>();
    const second = deferred<Option[]>();
    bffFetch.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(
      ({ enabled }) => useLazyFilterOptions<Option>({
        enabled,
        initialEnabled: false,
        path: "/bff/options",
      }),
      { initialProps: { enabled: false } },
    );

    act(() => result.current.start());
    rerender({ enabled: true });
    await waitFor(() => expect(bffFetch).toHaveBeenCalledTimes(1));
    const firstSignal = bffFetch.mock.calls[0]?.[1]?.signal as AbortSignal;

    rerender({ enabled: false });
    expect(firstSignal.aborted).toBe(true);
    rerender({ enabled: true });
    await waitFor(() => expect(bffFetch).toHaveBeenCalledTimes(2));

    second.resolve([{ key: "two" }]);
    await waitFor(() => expect(result.current.status).toBe("loaded"));
    expect(result.current.items).toEqual([{ key: "two" }]);
  });

  it("retries a failed request", async () => {
    bffFetch.mockRejectedValueOnce(new Error("failed"));
    bffFetch.mockResolvedValueOnce([{ key: "recovered" }]);
    const { result } = renderHook(() => useLazyFilterOptions<Option>({
      enabled: true,
      initialEnabled: true,
      path: "/bff/options",
    }));

    await waitFor(() => expect(result.current.status).toBe("error"));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe("loaded"));
    expect(result.current.items).toEqual([{ key: "recovered" }]);
    expect(bffFetch).toHaveBeenCalledTimes(2);
  });

  it("reuses loaded options after close and reopen", async () => {
    bffFetch.mockResolvedValue([{ key: "cached" }]);
    const { result, rerender } = renderHook(
      ({ enabled }) => useLazyFilterOptions<Option>({
        enabled,
        initialEnabled: true,
        path: "/bff/options",
      }),
      { initialProps: { enabled: true } },
    );

    await waitFor(() => expect(result.current.status).toBe("loaded"));
    rerender({ enabled: false });
    rerender({ enabled: true });
    expect(bffFetch).toHaveBeenCalledTimes(1);
  });
});
