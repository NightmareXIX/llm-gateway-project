/**
 * The guarantee `ProviderKeysSection.test.tsx` cannot see from the DOM: every
 * successful key write refreshes `/v1/models` as well as the key list.
 *
 * This is not defensive revalidation. §9.7 lets a private key unlock a slot
 * nobody else can be served (`pro`, on a Gemini Pro model the shared free-tier
 * key cannot reach), and §9.4 computes every *other* slot's status under the
 * caller's own quota scope — so adding or removing a key changes both what the
 * picker should offer and what each entry's status means. A picker still
 * showing the old list after a successful add is the one place a user would
 * conclude the feature does not work, and it would be invisible to any test
 * that only looked at the settings dialog.
 */

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MODELS_KEY, useProviderKeys } from "@/lib/hooks";

const {
  addProviderKey,
  removeProviderKey,
  revalidateProviderKey,
  globalMutate,
  localMutate,
  useSWR,
} = vi.hoisted(() => ({
  addProviderKey: vi.fn(async () => undefined),
  removeProviderKey: vi.fn(async () => undefined),
  revalidateProviderKey: vi.fn(async () => undefined),
  globalMutate: vi.fn(async () => undefined),
  localMutate: vi.fn(async () => undefined),
  useSWR: vi.fn(),
}));

vi.mock("swr", () => ({ default: useSWR, mutate: globalMutate }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { addProviderKey, removeProviderKey, revalidateProviderKey },
  swrFetcher: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  useSWR.mockReturnValue({
    data: [{ provider: "gemini", pool: "shared", key: null }],
    error: undefined,
    isLoading: false,
    mutate: localMutate,
  });
});

describe("useProviderKeys", () => {
  it("subscribes to the settings list", () => {
    const { result } = renderHook(() => useProviderKeys());

    expect(useSWR).toHaveBeenCalledWith("/v1/provider-keys", expect.anything(), expect.anything());
    expect(result.current.rows).toHaveLength(1);
  });

  it("revalidates the key list and the model list after an add", async () => {
    const { result } = renderHook(() => useProviderKeys());

    await act(async () => {
      await result.current.add("gemini", "AIzaSy-real");
    });

    expect(addProviderKey).toHaveBeenCalledWith("gemini", "AIzaSy-real");
    expect(localMutate).toHaveBeenCalled();
    expect(globalMutate).toHaveBeenCalledWith(MODELS_KEY);
  });

  it("does the same after a remove — the unlocked slot has to disappear again", async () => {
    const { result } = renderHook(() => useProviderKeys());

    await act(async () => {
      await result.current.remove("gemini");
    });

    expect(removeProviderKey).toHaveBeenCalledWith("gemini");
    expect(localMutate).toHaveBeenCalled();
    expect(globalMutate).toHaveBeenCalledWith(MODELS_KEY);
  });

  it("and after a re-check, which can move a row from invalid back to valid", async () => {
    const { result } = renderHook(() => useProviderKeys());

    await act(async () => {
      await result.current.revalidate("gemini");
    });

    expect(revalidateProviderKey).toHaveBeenCalledWith("gemini");
    expect(globalMutate).toHaveBeenCalledWith(MODELS_KEY);
  });

  it("lets a failed write reach the caller rather than swallowing it", async () => {
    // Which failure happened is the entire message on this surface — a 422 is
    // the provider's wording, a 503 is "we could not check", a 429 is a wait.
    // A hook that returned a boolean would flatten all three.
    addProviderKey.mockRejectedValueOnce(new Error("nope"));
    const { result } = renderHook(() => useProviderKeys());

    await expect(
      act(async () => {
        await result.current.add("gemini", "bad");
      }),
    ).rejects.toThrow("nope");

    expect(globalMutate).not.toHaveBeenCalled();
  });
});
