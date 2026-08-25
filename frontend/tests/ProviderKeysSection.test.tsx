/**
 * The BYOK settings surface, in the six states a user can actually land in.
 *
 * Written against §9.2 and §9.8 rather than against the markup: what a person
 * must be able to read and press in each state, not which element carries it.
 * The three that matter most are the ones the phase doc calls out by name —
 * a rejected key must say *nothing was saved*, an unreachable provider must
 * **not** say the key is bad, and a stored key the gateway later found broken
 * has to admit it rather than quietly reading "Using your key" forever.
 *
 * The hook's own guarantee — that every successful write also revalidates
 * `/v1/models` — is invisible from this DOM and is tested next door, in
 * `useProviderKeys.test.tsx`.
 */

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProviderKeysSection } from "@/components/ProviderKeysSection";
import { GatewayError } from "@/lib/api";
import type { ProviderKeyOut, ProviderKeyStatus } from "@/lib/types";

const { useProviderKeys } = vi.hoisted(() => ({ useProviderKeys: vi.fn() }));

vi.mock("@/lib/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/hooks")>()),
  useProviderKeys,
}));

function storedKey(overrides: Partial<ProviderKeyOut> = {}): ProviderKeyOut {
  return {
    provider: "gemini",
    masked: "••••a91c",
    nickname: null,
    validation_status: "valid",
    last_validated_at: "2026-08-26T00:00:00Z",
    last_used_at: "2026-08-26T00:05:00Z",
    is_active: true,
    created_at: "2026-08-26T00:00:00Z",
    ...overrides,
  };
}

const SHARED_ROWS: ProviderKeyStatus[] = [
  { provider: "gemini", pool: "shared", key: null },
  { provider: "groq", pool: "shared", key: null },
];

/** Wire the mocked hook up with a given list and a set of spies. */
function mount(
  rows: ProviderKeyStatus[],
  writes: Partial<{
    add: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
    revalidate: ReturnType<typeof vi.fn>;
  }> = {},
) {
  const spies = {
    add: writes.add ?? vi.fn(async () => undefined),
    remove: writes.remove ?? vi.fn(async () => undefined),
    revalidate: writes.revalidate ?? vi.fn(async () => undefined),
  };
  useProviderKeys.mockReturnValue({ rows, error: undefined, isLoading: false, ...spies });
  render(<ProviderKeysSection />);
  return spies;
}

/** Open one row's form and submit a key into it. */
async function addKeyTo(provider: string, key = "AIzaSy-a-real-looking-key") {
  const row = within(screen.getByRole("group", { name: provider }));
  fireEvent.click(row.getByRole("button", { name: "Add key" }));
  fireEvent.change(row.getByPlaceholderText(`Paste your ${provider} key`), {
    target: { value: key },
  });
  await act(async () => {
    fireEvent.click(row.getByRole("button", { name: /save/i }));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("state 1 — no key stored", () => {
  it("renders one row per enabled provider, all on the shared pool", () => {
    // The list comes from the gateway, so a settings page with nothing on it is
    // still a complete one — the client never has to know the provider set.
    mount(SHARED_ROWS);

    expect(screen.getByText("Google")).toBeInTheDocument();
    expect(screen.getByText("Groq")).toBeInTheDocument();
    expect(screen.getAllByText("Using shared pool")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Add key" })).toHaveLength(2);
  });

  it("carries §9.8's disclosure above the rows, in plain language", () => {
    mount(SHARED_ROWS);

    // The trade being agreed to, not a tooltip: the request leaves under the
    // user's own account and the provider's terms, not ours.
    expect(screen.getByText(/billed to you, and governed by their terms/)).toBeInTheDocument();
  });

  it("shows no input until Add is pressed", () => {
    mount(SHARED_ROWS);

    expect(screen.queryByPlaceholderText(/Paste your/)).not.toBeInTheDocument();
  });
});

describe("state 2 — adding", () => {
  it("takes the key through a masked input that is never pre-filled", async () => {
    const add = vi.fn(async () => undefined);
    mount(SHARED_ROWS, { add });

    const row = within(screen.getByRole("group", { name: "Google" }));
    fireEvent.click(row.getByRole("button", { name: "Add key" }));
    const input = row.getByPlaceholderText("Paste your Google key") as HTMLInputElement;

    // `password`, because this is a live credential typed into a dialog that is
    // routinely open on a shared screen.
    expect(input.type).toBe("password");
    expect(input.value).toBe("");

    fireEvent.change(input, { target: { value: "AIzaSy-real" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /save/i }));
    });

    expect(add).toHaveBeenCalledWith("gemini", "AIzaSy-real");
  });

  it("clears the box and closes the form on success", async () => {
    mount(SHARED_ROWS);
    await addKeyTo("Google");

    // The row itself only flips once the refetched list says so; what this
    // component owes is that the plaintext is gone from the DOM either way.
    expect(screen.queryByDisplayValue(/AIzaSy/)).not.toBeInTheDocument();
  });
});

describe("state 3 — the provider rejected the key", () => {
  const rejected = new GatewayError({
    status: 422,
    code: "invalid_provider_key",
    message: "Google rejected this key: API key not valid.",
    requestId: "01JABCDEF",
  });

  it("renders the provider's own wording inline, and says nothing was stored", async () => {
    // §9.2: the add flow validates *before* it stores, so "nothing was saved"
    // is a fact worth stating — a user who assumes otherwise will go hunting
    // for a key to remove.
    mount(SHARED_ROWS, { add: vi.fn(async () => Promise.reject(rejected)) });
    await addKeyTo("Google");

    expect(screen.getByRole("alert")).toHaveTextContent("API key not valid.");
    expect(screen.getByRole("alert")).toHaveTextContent("Nothing was saved.");
  });

  it("clears the box on failure too, not just on success", async () => {
    // Otherwise the same bad string sits there inviting a second submit,
    // against a route that allows five attempts an hour.
    mount(SHARED_ROWS, { add: vi.fn(async () => Promise.reject(rejected)) });
    await addKeyTo("Google", "AIzaSy-bad");

    expect(screen.queryByDisplayValue("AIzaSy-bad")).not.toBeInTheDocument();
  });

  it("does not say the key is bad when the provider was merely unreachable", async () => {
    // Trap 6, and the whole reason the gateway raises two different codes: a
    // 503 means we could not check, and telling someone their key is invalid
    // because Google was down is the confusion §9.2 exists to prevent.
    const down = new GatewayError({
      status: 503,
      code: "provider_unavailable",
      message: "Could not verify this key right now — the provider was unreachable.",
      requestId: "01JABCDEF",
    });
    mount(SHARED_ROWS, { add: vi.fn(async () => Promise.reject(down)) });
    await addKeyTo("Google");

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Could not reach Google to check this key");
    expect(alert).not.toHaveTextContent(/rejected/);
  });
});

describe("state 4 — rate limited", () => {
  it("renders D43's floor as a wait, with the gateway's own number", async () => {
    const limited = new GatewayError({
      status: 429,
      code: "rate_limited",
      message: "Too many requests.",
      requestId: "01JABCDEF",
      retryAfterS: 1800,
    });
    mount(SHARED_ROWS, { add: vi.fn(async () => Promise.reject(limited)) });
    await addKeyTo("Google");

    // A wait, not a rejection — and `Retry-After` is solved for server-side,
    // so the number is reused rather than guessed.
    expect(screen.getByRole("alert")).toHaveTextContent("Try again in about 30 min");
  });
});

describe("state 5 — an active key", () => {
  const rows: ProviderKeyStatus[] = [
    { provider: "gemini", pool: "private", key: storedKey() },
    { provider: "groq", pool: "shared", key: null },
  ];

  it("says the key is in use and shows only the masked tail", () => {
    mount(rows);

    expect(screen.getByText(/Using your key/)).toBeInTheDocument();
    expect(screen.getByText("••••a91c")).toBeInTheDocument();
    // There is no endpoint that returns a stored key's plaintext, and this is
    // the surface that would leak one if there were.
    expect(screen.queryByPlaceholderText(/Paste your Google key/)).not.toBeInTheDocument();
  });

  it("offers Remove instead of Add, and removes that provider", async () => {
    const remove = vi.fn(async () => undefined);
    mount(rows, { remove });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });

    expect(remove).toHaveBeenCalledWith("gemini");
  });
});

describe("state 6 — a stored key the gateway found broken (D40)", () => {
  const rows: ProviderKeyStatus[] = [
    { provider: "gemini", pool: "private", key: storedKey({ validation_status: "invalid" }) },
  ];

  it("warns that the provider rejected it, rather than reading as healthy", () => {
    // The write D40 added exists precisely so this row can say something. A
    // settings page that reads "Using your key" while every answer comes from
    // the shared pool is the failure that decision exists to prevent.
    mount(rows);

    expect(screen.getByText(/rejected this key the last time it was used/)).toBeInTheDocument();
  });

  it("offers a re-check, not only a removal", async () => {
    // A key rejected during a provider outage is worth asking about twice.
    const revalidate = vi.fn(async () => undefined);
    mount(rows, { revalidate });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    });

    expect(revalidate).toHaveBeenCalledWith("gemini");
  });
});
