/**
 * The dashboard, in the two states that matter and the several rules that
 * are easy to break quietly.
 *
 * Written against D44–D46 rather than against the markup: what an account
 * holder must be able to read, and what the page must never claim. The two
 * headline cases are a populated window (every panel present, every number
 * traceable to the mocked response) and a brand-new account (every panel
 * showing its own empty state, and not one `NaN` anywhere) — the second being
 * the first thing a reviewer sees, and therefore the one worth a test of its
 * own.
 *
 * The hook-level guarantee — that switching windows changes the SWR *key*
 * rather than refetching one entry — is invisible from this DOM and is tested
 * next door in `useUsage.test.tsx`, the same split `ProviderKeysSection` and
 * `useProviderKeys` already model.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UsageDashboard, formatCost, quotaRows } from "@/components/UsageDashboard";
import type {
  ModelsResponse,
  RequestRow,
  UsageOverview,
  UsageWindow,
} from "@/lib/types";

const { useUsage, useQuotaOverview, useRecentRequests } = vi.hoisted(() => ({
  useUsage: vi.fn(),
  useQuotaOverview: vi.fn(),
  useRecentRequests: vi.fn(),
}));

vi.mock("@/lib/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/hooks")>()),
  useUsage,
  useQuotaOverview,
  useRecentRequests,
}));

// --------------------------------------------------------------------------- //
// Fixtures
// --------------------------------------------------------------------------- //
const EMPTY_USAGE: UsageOverview = {
  window: "24h",
  since: "2026-08-31T00:00:00Z",
  until: "2026-09-01T00:00:00Z",
  outcomes: {
    total: 0,
    ok: 0,
    errors: 0,
    cache_hits: 0,
    replays: 0,
    substituted: 0,
    multi_attempt: 0,
    tokens_in: 0,
    tokens_out: 0,
    wasted_tokens_out: 0,
  },
  // The buckets exist even when nothing happened in them (D45, trap 8) — a
  // zero-request account still gets a full series of zeroes off the wire.
  volume: [0, 0, 0].map((_, index) => ({
    bucket_start: `2026-08-31T0${index}:00:00Z`,
    total: 0,
    errors: 0,
    cache_hits: 0,
  })),
  providers: [],
  pool_split: {
    shared_requests: 0,
    shared_tokens_in: 0,
    shared_tokens_out: 0,
    shared_cost: null,
    private_requests: 0,
    private_tokens_in: 0,
    private_tokens_out: 0,
    private_cost: null,
  },
  total_cost: null,
  currency: null,
  unpriced_requests: 0,
};

const BUSY_USAGE: UsageOverview = {
  ...EMPTY_USAGE,
  outcomes: {
    total: 100,
    ok: 88,
    errors: 4,
    cache_hits: 6,
    replays: 2,
    substituted: 9,
    multi_attempt: 12,
    tokens_in: 40_000,
    tokens_out: 12_000,
    wasted_tokens_out: 500,
  },
  volume: [
    { bucket_start: "2026-08-31T00:00:00Z", total: 40, errors: 2, cache_hits: 1 },
    { bucket_start: "2026-08-31T01:00:00Z", total: 0, errors: 0, cache_hits: 0 },
    { bucket_start: "2026-08-31T02:00:00Z", total: 60, errors: 2, cache_hits: 5 },
  ],
  providers: [
    {
      provider: "groq",
      model: "openai/gpt-oss-120b",
      requests: 70,
      tokens_in: 30_000,
      tokens_out: 9_000,
      simulated_cost: "0.2500",
    },
    {
      provider: "gemini",
      model: "gemini-3.6-flash",
      requests: 24,
      tokens_in: 10_000,
      tokens_out: 3_000,
      simulated_cost: null,
    },
  ],
  pool_split: {
    shared_requests: 80,
    shared_tokens_in: 30_000,
    shared_tokens_out: 9_000,
    shared_cost: "0.2000",
    private_requests: 14,
    private_tokens_in: 10_000,
    private_tokens_out: 3_000,
    private_cost: "0.0500",
  },
  total_cost: "0.2500",
  currency: "USD",
  unpriced_requests: 24,
};

const QUOTA: ModelsResponse = {
  object: "list",
  data: [
    {
      id: "fast",
      object: "model",
      created: 0,
      owned_by: "groq",
      status: "available",
      resets_at: null,
      description: "",
      candidates: [
        {
          provider: "groq",
          model: "openai/gpt-oss-120b",
          status: "available",
          breaker_state: "closed",
          resets_at: null,
          windows: [
            { window: "rpm", limit: 30, remaining: 29, resets_at: "2026-09-01T00:01:00Z" },
            { window: "rpd", limit: 1000, remaining: 750, resets_at: "2026-09-02T00:00:00Z" },
          ],
        },
      ],
    },
    {
      id: "auto",
      object: "model",
      created: 0,
      owned_by: null,
      status: "available",
      resets_at: null,
      description: "",
      // The same candidate again — `auto` lists the whole fleet, and the panel
      // must not draw it twice.
      candidates: [
        {
          provider: "groq",
          model: "openai/gpt-oss-120b",
          status: "available",
          breaker_state: "closed",
          resets_at: null,
          windows: [
            { window: "rpd", limit: 1000, remaining: 750, resets_at: "2026-09-02T00:00:00Z" },
          ],
        },
        {
          provider: "gemini",
          model: "gemini-3.6-flash",
          status: "rate_limited",
          breaker_state: "closed",
          resets_at: "2026-09-02T00:00:00Z",
          windows: [
            { window: "rpd", limit: 200, remaining: 0, resets_at: "2026-09-02T00:00:00Z" },
          ],
        },
      ],
    },
  ],
};

function requestRow(overrides: Partial<RequestRow> = {}): RequestRow {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    created_at: "2026-09-01T00:00:00Z",
    requested_slot: "general",
    served_slot: "general",
    provider: "groq",
    model: "openai/gpt-oss-120b",
    status: "ok",
    tokens_in: 120,
    tokens_out: 40,
    latency_ms: 812,
    ttft_ms: 210,
    cache_hit: false,
    substituted: false,
    error_code: null,
    quota_scope: "system",
    ...overrides,
  };
}

function mount({
  usage = BUSY_USAGE,
  quota = QUOTA,
  requests = [requestRow()],
}: {
  usage?: UsageOverview | undefined;
  quota?: ModelsResponse | undefined;
  requests?: RequestRow[] | undefined;
} = {}) {
  useUsage.mockReturnValue({ usage, error: undefined, isLoading: false, mutate: vi.fn() });
  useQuotaOverview.mockReturnValue({ quota, error: undefined, isLoading: false });
  useRecentRequests.mockReturnValue({ requests, error: undefined, isLoading: false });
  render(<UsageDashboard />);
}

beforeEach(() => {
  vi.clearAllMocks();
});

// --------------------------------------------------------------------------- //
// A populated window
// --------------------------------------------------------------------------- //
describe("UsageDashboard, with traffic", () => {
  it("renders every panel", () => {
    mount();

    for (const title of [
      "Requests over time",
      "Outcomes",
      "Who served it",
      "Simulated cost",
      "What's left",
      "Recent calls",
    ]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    }
  });

  it("reports the three outcome rates over the window's own total", () => {
    mount();

    // 4/100, 6/100, 9/100 — computed against every request in the window,
    // failures and cache hits included, exactly as D45 specifies.
    expect(screen.getByRole("img", { name: "Error rate: 4%" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Cache hit rate: 6%" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Failover rate: 9%" })).toBeInTheDocument();
  });

  it("counts a replay on its own axis rather than as an error", () => {
    mount();

    // trap 18: `total` partitions into ok + errors + replays, so a successful
    // idempotent retry must never inflate the error rate.
    const volume = panel("Requests over time");
    expect(within(volume).getByText("Replayed").nextSibling).toHaveTextContent("2");
    expect(within(volume).getByText("Failed").nextSibling).toHaveTextContent("4");
  });

  it("names each provider slice and marks the unpriced one as unpriced, not free", () => {
    mount();
    const served = panel("Who served it");

    expect(within(served).getByText(/Groq · GPT-OSS 120B/)).toBeInTheDocument();
    expect(within(served).getByText(/Google · Gemini 3.6 Flash/)).toBeInTheDocument();
    expect(within(served).getByText(/No price on file/)).toBeInTheDocument();
    // trap 7 again, in the words the panel uses.
    expect(screen.getByText(/an unpriced model is unpriced, not free/i)).toBeInTheDocument();
  });

  it("shows the cost, its currency, and the count it excludes", () => {
    mount();
    const cost = panel("Simulated cost");

    expect(within(cost).getByText("$0.25")).toBeInTheDocument();
    expect(within(cost).getByText("24 requests unpriced")).toBeInTheDocument();
    // The disclosure is on the page, not in a tooltip: the number is a fiction
    // computed at read time and the panel has to say so (D46).
    expect(within(cost).getByText(/nothing here was billed/i)).toBeInTheDocument();
  });

  it("splits the cost between the shared pool and the caller's own keys", () => {
    mount();
    const cost = panel("Simulated cost");

    expect(within(cost).getByText("$0.20")).toBeInTheDocument();
    expect(within(cost).getByText("80 requests")).toBeInTheDocument();
    expect(within(cost).getByText("$0.05")).toBeInTheDocument();
    expect(within(cost).getByText("14 requests")).toBeInTheDocument();
    expect(within(cost).getByText(/approximation rather than a ledger/i)).toBeInTheDocument();
  });

  it("draws one quota row per candidate, not one per slot that offers it", () => {
    mount();
    const left = panel("What's left");

    // Groq appears under both `fast` and `auto`; it is one budget either way.
    expect(within(left).getAllByRole("listitem")).toHaveLength(2);
    expect(within(left).getByText("250 / 1,000 rpd")).toBeInTheDocument();
    expect(within(left).getByText("200 / 200 rpd")).toBeInTheDocument();
  });

  it("labels a cache hit as one instead of claiming a provider call", () => {
    // trap 5's client half: the row names the candidate that *originally*
    // answered, so rendering it as an ordinary call would report a request
    // that never went out.
    mount({ requests: [requestRow({ cache_hit: true, tokens_in: 0, tokens_out: 0 })] });

    expect(within(panel("Recent calls")).getByText("from cache")).toBeInTheDocument();
  });

  it("renders a request that never reached a provider as an em dash", () => {
    // trap 6: NULL is "never got that far", not a provider called "unknown".
    mount({
      requests: [
        requestRow({
          provider: null,
          model: null,
          status: "error",
          error_code: "all_providers_failed",
          tokens_in: null,
          tokens_out: null,
          latency_ms: null,
        }),
      ],
    });

    const row = within(panel("Recent calls")).getAllByRole("row")[1]!;
    expect(within(row).getByText("all_providers_failed")).toBeInTheDocument();
    expect(within(row).getAllByText("—").length).toBeGreaterThan(0);
  });

  it("treats a replayed row in the table as a success, like the aggregate does", () => {
    mount({ requests: [requestRow({ status: "replayed", provider: null, model: null })] });

    const pill = within(panel("Recent calls")).getByText("replayed");
    expect(pill.className).not.toContain("text-danger");
  });
});

// --------------------------------------------------------------------------- //
// A brand-new account
// --------------------------------------------------------------------------- //
describe("UsageDashboard, with no traffic at all", () => {
  it("shows an empty state in every panel and never renders NaN", () => {
    mount({ usage: EMPTY_USAGE, quota: { object: "list", data: [] }, requests: [] });

    expect(within(panel("Requests over time")).getByText("Nothing yet")).toBeInTheDocument();
    expect(within(panel("Who served it")).getByText("No provider calls yet")).toBeInTheDocument();
    expect(within(panel("What's left")).getByText("No budget to report")).toBeInTheDocument();
    expect(within(panel("Recent calls")).getByText("No calls yet")).toBeInTheDocument();

    expect(document.body.textContent).not.toContain("NaN");
    expect(document.body.textContent).not.toContain("Infinity");
  });

  it("renders every rate as no-data rather than as a zero percent it cannot justify", () => {
    mount({ usage: EMPTY_USAGE, quota: { object: "list", data: [] }, requests: [] });

    expect(screen.getByRole("img", { name: "Error rate: no data" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Cache hit rate: no data" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Failover rate: no data" })).toBeInTheDocument();
  });

  it("prints an unpriced-and-unspent cost as an em dash rather than as zero", () => {
    mount({ usage: EMPTY_USAGE, quota: { object: "list", data: [] }, requests: [] });

    const cost = panel("Simulated cost");
    expect(within(cost).getAllByText("—").length).toBe(3);
    expect(within(cost).queryByText(/unpriced$/)).toBeNull();
  });
});

// --------------------------------------------------------------------------- //
// The window switch
// --------------------------------------------------------------------------- //
describe("the window switch", () => {
  it("asks the hook for the window that was pressed", () => {
    mount();

    expect(useUsage).toHaveBeenLastCalledWith("24h");
    fireEvent.click(screen.getByRole("radio", { name: "7 days" }));
    expect(useUsage).toHaveBeenLastCalledWith("7d");
  });

  it("marks the chosen window and no other", () => {
    mount();
    fireEvent.click(screen.getByRole("radio", { name: "Last hour" }));

    const chosen = screen.getAllByRole("radio").filter((radio) =>
      radio.getAttribute("aria-checked") === "true",
    );
    expect(chosen).toHaveLength(1);
    expect(chosen[0]).toHaveAccessibleName("Last hour");
  });

  it("carries the window into the empty state's wording", () => {
    mount({ usage: { ...EMPTY_USAGE, window: "1h" satisfies UsageWindow } });
    fireEvent.click(screen.getByRole("radio", { name: "Last hour" }));

    expect(screen.getByText(/No requests in the last hour/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------- //
// The two exported pure helpers
// --------------------------------------------------------------------------- //
describe("formatCost", () => {
  it("formats a decimal string in its own currency", () => {
    expect(formatCost("0.2500", "USD")).toBe("$0.25");
  });

  it("keeps the small end of the range visible", () => {
    expect(formatCost("0.0001", "USD")).toBe("$0.0001");
  });

  it("renders no price on file as an em dash, never as zero", () => {
    expect(formatCost(null, "USD")).toBe("—");
  });

  it("survives a currency code Intl does not recognise", () => {
    expect(formatCost("1.5", "XYZZY")).toBe("1.5000 XYZZY");
  });

  it("falls back to a bare number when there is no currency to denominate in", () => {
    expect(formatCost("1.5", null)).toBe("1.5000");
  });
});

describe("quotaRows", () => {
  it("prefers the daily window a person can act on", () => {
    const rows = quotaRows(QUOTA);
    expect(rows.map((row) => row.window.window)).toEqual(["rpd", "rpd"]);
    // Attributed to the first slot that offered it, not to `auto`.
    expect(rows[0]!.slot).toBe("fast");
  });

  it("falls back to whatever window is tracked when there is no rpd", () => {
    const rows = quotaRows(withWindows([{ window: "rpm", limit: 30, remaining: 30, resets_at: "" }]));
    expect(rows[0]!.window.window).toBe("rpm");
  });

  it("skips a candidate with no tracked window rather than drawing an empty bar", () => {
    expect(quotaRows(withWindows([]))).toHaveLength(0);
  });
});

function withWindows(windows: ModelsResponse["data"][number]["candidates"][number]["windows"]) {
  return {
    object: "list",
    data: [{ ...QUOTA.data[0]!, candidates: [{ ...QUOTA.data[0]!.candidates[0]!, windows }] }],
  } satisfies ModelsResponse;
}

/** The section a panel heading belongs to — the addressable unit on this page,
 *  the way each provider-key row is its own labelled group. */
function panel(title: string): HTMLElement {
  return screen.getByRole("heading", { name: title }).closest("section")!;
}
