/**
 * The guarantee `UsageDashboard.test.tsx` cannot see from the DOM: the window
 * is part of the **SWR key**, not a parameter a single cache entry is refetched
 * under.
 *
 * That distinction is the whole reason `usageKey` exists. Each window is an
 * independent read-only document, so keying on it means switching back to a
 * window already fetched is instant, and — the part that would actually be a
 * bug — there is never a frame in which the heading says "last 7 days" over
 * numbers still computed for the last hour, which is exactly what a shared
 * entry being revalidated in place would produce.
 *
 * The same split as `ProviderKeysSection` / `useProviderKeys`: this file mocks
 * `swr` and uses the real hooks, where that one mocks the hooks and uses the
 * real DOM.
 */

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ADMIN_QUOTA_KEY, ADMIN_REQUESTS_KEY, usageKey } from "@/lib/api";
import { MODELS_KEY, useQuotaOverview, useRecentRequests, useUsage } from "@/lib/hooks";
import type { UsageWindow } from "@/lib/types";

const { useSWR } = vi.hoisted(() => ({ useSWR: vi.fn() }));

vi.mock("swr", () => ({ default: useSWR, mutate: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));

beforeEach(() => {
  vi.clearAllMocks();
  useSWR.mockReturnValue({ data: undefined, error: undefined, isLoading: true, mutate: vi.fn() });
});

describe("useUsage", () => {
  it("keys on the window it was asked for", () => {
    renderHook(() => useUsage("24h"));

    expect(useSWR).toHaveBeenCalledWith(
      "/v1/admin/usage?window=24h",
      expect.anything(),
      expect.anything(),
    );
  });

  it("subscribes to a different key for each window", () => {
    const { rerender } = renderHook<unknown, { window: UsageWindow }>(
      ({ window }) => useUsage(window),
      { initialProps: { window: "24h" } },
    );

    rerender({ window: "7d" });

    const keys = useSWR.mock.calls.map((call) => call[0]);
    expect(keys).toContain(usageKey("24h"));
    expect(keys).toContain(usageKey("7d"));
    expect(usageKey("24h")).not.toBe(usageKey("7d"));
  });

  it("passes the response straight through", () => {
    useSWR.mockReturnValue({
      data: { window: "1h" },
      error: undefined,
      isLoading: false,
      mutate: vi.fn(),
    });

    const { result } = renderHook(() => useUsage("1h"));
    expect(result.current.usage).toEqual({ window: "1h" });
  });
});

describe("useQuotaOverview", () => {
  it("reads the admin route, which is deliberately a different cache entry from the picker's", () => {
    // Same answer, same handler — but the picker's copy is revalidated after
    // every turn and every key write, and the dashboard's is a point-in-time
    // read of a page the user opened. Sharing one entry would make each
    // surface's refresh policy the other's.
    renderHook(() => useQuotaOverview());

    expect(useSWR).toHaveBeenCalledWith(ADMIN_QUOTA_KEY, expect.anything(), expect.anything());
    expect(ADMIN_QUOTA_KEY).not.toBe(MODELS_KEY);
  });
});

describe("useRecentRequests", () => {
  it("unwraps the envelope the route returns", () => {
    useSWR.mockReturnValue({
      data: { data: [{ id: "a" }] },
      error: undefined,
      isLoading: false,
      mutate: vi.fn(),
    });

    const { result } = renderHook(() => useRecentRequests());

    expect(useSWR).toHaveBeenCalledWith(ADMIN_REQUESTS_KEY, expect.anything(), expect.anything());
    expect(result.current.requests).toEqual([{ id: "a" }]);
  });
});
