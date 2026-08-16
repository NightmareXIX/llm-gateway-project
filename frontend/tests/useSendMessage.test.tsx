/**
 * The client-side half of D1, tested where it actually lives.
 *
 * §1.1's client contract is one sentence — *on `restart`, clear the in-progress
 * bubble entirely; do not splice or diff the two attempts* — and it is the single
 * easiest thing in this phase to get wrong in a way that looks like a model
 * problem rather than a client bug. Half an answer from Groq welded to a full
 * answer from Gemini reads as a model that repeats itself, and whoever
 * investigates will start with the provider.
 *
 * So the clearing is asserted directly, on the hook that does it, rather than
 * inferred from a component that happens to render the right string today.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSendMessage } from "@/lib/hooks";
import type { DoneEvent, MetaEvent, RestartEvent, StreamHandlers } from "@/lib/sse";

// `vi.hoisted` because `vi.mock` factories are lifted above every other
// statement in the file, so a plain `const` above them is still in its temporal
// dead zone by the time they run.
const { renameConversation, replace, openCompletionStream } = vi.hoisted(() => ({
  renameConversation: vi.fn(async () => undefined),
  replace: vi.fn(),
  openCompletionStream: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("swr", () => ({ default: vi.fn(), mutate: vi.fn(async () => undefined) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { renameConversation },
  swrFetcher: vi.fn(),
}));
vi.mock("@/lib/sse", () => ({ openCompletionStream }));

const META: MetaEvent = {
  attempt: 1,
  slot: "general",
  provider: "groq",
  model: "llama-3.3-70b-versatile",
  requested_slot: "auto",
  conversation_id: "8b0d1f6e-0000-4000-8000-000000000000",
  message_id: "8b0d1f6e-0000-4000-8000-000000000001",
};

const RESTART: RestartEvent = {
  reason: "provider_unavailable",
  failed: { provider: "groq", model: "llama-3.3-70b-versatile" },
  next: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
  attempt: 2,
  discarded_chars: 24,
};

const DONE: DoneEvent = {
  served_by: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
  requested_slot: "auto",
  substituted: false,
  attempts: 2,
  usage: {
    prompt_tokens: 812,
    completion_tokens: 340,
    total_tokens: 1152,
    estimated: false,
    wasted_tokens_out: 96,
  },
  degraded: false,
  status: "ok",
};

/** Drive the hook with a scripted event sequence, as the gateway would. */
function script(play: (handlers: StreamHandlers) => void | Promise<void>) {
  openCompletionStream.mockImplementation(
    async (_body: unknown, handlers: StreamHandlers) => await play(handlers),
  );
}

/**
 * A scripted stream that stops mid-flight and waits.
 *
 * Necessary because the interesting state here is *in-flight*: a completed turn
 * clears `pending` and a failed one clears the bubble, so asserting after the
 * stream is over would be asserting about the wrong moment entirely.
 */
function pausedScript(play: (handlers: StreamHandlers) => void) {
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));

  script(async (handlers) => {
    play(handlers);
    await held;
    handlers.onDone?.(DONE);
  });

  return { release: () => release() };
}

const delta = (content: string) => ({ choices: [{ delta: { content } }] });

beforeEach(() => vi.clearAllMocks());

describe("a restart", () => {
  it("clears the bubble instead of splicing the two attempts", async () => {
    const stream = pausedScript((handlers) => {
      handlers.onMeta?.(META);
      handlers.onDelta?.(delta("Groq began writing thi"));
      handlers.onRestart?.(RESTART);
      handlers.onDelta?.(delta("Gemini wrote the whole answer."));
    });

    const { result } = renderHook(() => useSendMessage("conv-1"));
    let sent!: Promise<void>;
    await act(async () => {
      sent = result.current.send("what is the capital of france?");
    });

    // The discarded attempt is *gone*, not prefixed, not deduplicated.
    expect(result.current.pending?.answer).toBe("Gemini wrote the whole answer.");
    expect(result.current.pending?.answer).not.toContain("Groq");

    await act(async () => {
      stream.release();
      await sent;
    });
  });

  it("swaps the indicator to the model that took over", async () => {
    const stream = pausedScript((handlers) => {
      handlers.onMeta?.(META);
      handlers.onDelta?.(delta("half"));
      handlers.onRestart?.(RESTART);
      handlers.onDelta?.(delta("whole"));
    });

    const { result } = renderHook(() => useSendMessage("conv-1"));
    let sent!: Promise<void>;
    await act(async () => {
      sent = result.current.send("hi");
    });

    const provenance = result.current.pending?.provenance;
    expect(provenance?.servedBy).toEqual(RESTART.next);
    expect(provenance?.attempts).toBe(2);
    // The trail comes from the restarts this client watched happen — `done`
    // carries a count and no history.
    expect(provenance?.attemptTrail?.[0]).toMatchObject({
      provider: "groq",
      outcome: "failed",
      reason: "provider unavailable",
    });

    await act(async () => {
      stream.release();
      await sent;
    });
  });

  it("shows the streaming answer while it is still being written", async () => {
    const stream = pausedScript((handlers) => {
      handlers.onMeta?.(META);
      handlers.onDelta?.(delta("Hel"));
      handlers.onDelta?.(delta("lo"));
    });

    const { result } = renderHook(() => useSendMessage("conv-1"));
    let sent!: Promise<void>;
    await act(async () => {
      sent = result.current.send("hi");
    });

    expect(result.current.pending?.status).toBe("streaming");
    expect(result.current.pending?.answer).toBe("Hello");
    // `meta` is what the indicator renders from until `done` arrives — without it
    // the model name would appear only once the answer was already complete.
    expect(result.current.pending?.provenance?.servedBy.provider).toBe("groq");

    await act(async () => {
      stream.release();
      await sent;
    });
  });
});

describe("the terminal states", () => {
  it("clears the pending turn once the answer is stored", async () => {
    script((handlers) => {
      handlers.onMeta?.(META);
      handlers.onDelta?.(delta("Hello."));
      handlers.onDone?.(DONE);
    });

    const { result } = renderHook(() => useSendMessage("conv-1"));
    await act(async () => {
      await result.current.send("hi");
    });

    // The optimistic transcript row has taken over; there is nothing pending.
    await waitFor(() => expect(result.current.pending).toBeNull());
  });

  it("offers the partial when every attempt failed", async () => {
    const partial = "I was about to say something rather long and then the provider died";
    script((handlers) => {
      handlers.onMeta?.(META);
      handlers.onDelta?.(delta(partial));
      handlers.onDone?.({ ...DONE, status: "failed", partial_content: partial });
    });

    const { result } = renderHook(() => useSendMessage("conv-1"));
    await act(async () => {
      await result.current.send("hi");
    });

    expect(result.current.pending?.status).toBe("failed");
    expect(result.current.pending?.partial).toBe(partial);
    // Not in the bubble yet — keeping an unfinished thought is the user's call.
    expect(result.current.pending?.answer).toBe("");

    act(() => result.current.keepPartial());

    expect(result.current.pending?.status).toBe("unsaved");
    expect(result.current.pending?.answer).toBe(partial);
  });

  it("treats a stop as unsaved text, not as a failure", async () => {
    // The gateway persists nothing for a client that went away, so the honest
    // state is "on screen, not stored" — an error card would be a lie about what
    // happened, and clearing the text would throw away what the user stopped for.
    const { result } = renderHook(() => useSendMessage("conv-1"));
    openCompletionStream.mockImplementation(
      async (_body: unknown, handlers: StreamHandlers, signal: AbortSignal) => {
        handlers.onMeta?.(META);
        handlers.onDelta?.(delta("partial thought"));
        result.current.stop();
        expect(signal.aborted).toBe(true);
      },
    );

    await act(async () => {
      await result.current.send("hi");
    });

    expect(result.current.pending?.status).toBe("unsaved");
    expect(result.current.pending?.answer).toBe("partial thought");
  });

  it("surfaces a pre-stream failure as an error, envelope intact", async () => {
    const { GatewayError } = await import("@/lib/api");
    openCompletionStream.mockImplementation(async () => {
      throw new GatewayError({
        status: 502,
        code: "provider_unavailable",
        message: "Every model provider is unavailable.",
        requestId: "01JABCDEF",
      });
    });

    const { result } = renderHook(() => useSendMessage("conv-1"));
    await act(async () => {
      await result.current.send("hi");
    });

    expect(result.current.pending?.status).toBe("failed");
    expect((result.current.pending?.error as InstanceType<typeof GatewayError>).requestId).toBe(
      "01JABCDEF",
    );
  });
});
