/**
 * The provenance adapters for the streamed path.
 *
 * The first test here is the one that earns its keep: `sse.ts` claims `DoneEvent`
 * is structurally the non-streaming response's provenance block, and that claim
 * is the entire reason `ModelIndicator`'s contract could be frozen in Phase 1.
 * Asserting it means a future field added to one and not the other is a red test
 * rather than a component that quietly renders less for streamed answers.
 */

import { describe, expect, it } from "vitest";

import { buildAttemptTrail, fromCompletion, fromDoneEvent } from "@/lib/provenance";
import type { DoneEvent, MetaEvent, RestartEvent } from "@/lib/sse";
import type { ChatCompletionResponse } from "@/lib/types";

const META: MetaEvent = {
  attempt: 1,
  slot: "general",
  provider: "groq",
  model: "openai/gpt-oss-120b",
  requested_slot: "auto",
  conversation_id: "8b0d1f6e-0000-4000-8000-000000000000",
  message_id: "8b0d1f6e-0000-4000-8000-000000000001",
};

const RESTART: RestartEvent = {
  reason: "provider_unavailable",
  failed: { provider: "groq", model: "openai/gpt-oss-120b" },
  next: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
  attempt: 2,
  discarded_chars: 412,
};

describe("fromDoneEvent", () => {
  it("agrees field-for-field with the non-streaming adapter", () => {
    const facts = {
      served_by: { slot: "general", provider: "groq", model: "openai/gpt-oss-120b" },
      requested_slot: "auto",
      substituted: false,
      attempts: 1,
      degraded: false,
    };

    const done: DoneEvent = {
      ...facts,
      usage: { prompt_tokens: 812, completion_tokens: 340, total_tokens: 1152, estimated: false },
      status: "ok",
    };

    const response: ChatCompletionResponse = {
      ...facts,
      id: "01JABCDEF",
      object: "chat.completion",
      created: 1_760_000_000,
      model: "openai/gpt-oss-120b",
      choices: [{ index: 0, message: { role: "assistant", content: "hi" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 812, completion_tokens: 340, total_tokens: 1152, estimated: false },
      conversation_id: META.conversation_id,
      message_id: META.message_id,
    };

    // Same answer, two transports, one render. If this ever diverges, the
    // indicator is showing a streamed answer less than it shows a stored one.
    expect(fromDoneEvent(done)).toEqual({ ...fromCompletion(response), attemptTrail: undefined });
  });

  it("carries the tokens a discarded attempt really spent", () => {
    const done: DoneEvent = {
      served_by: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
      requested_slot: "fast",
      substituted: true,
      attempts: 2,
      usage: {
        prompt_tokens: 812,
        completion_tokens: 340,
        total_tokens: 1152,
        estimated: true,
        wasted_tokens_out: 96,
      },
      degraded: false,
      status: "ok",
    };

    expect(fromDoneEvent(done).wastedTokensOut).toBe(96);
    expect(fromDoneEvent(done).substituted).toBe(true);
  });
});

describe("buildAttemptTrail", () => {
  it("is a single served attempt when nothing went wrong", () => {
    expect(buildAttemptTrail(META, [], "ok")).toEqual([
      {
        attempt: 1,
        provider: "groq",
        model: "openai/gpt-oss-120b",
        slot: "general",
        outcome: "served",
      },
    ]);
  });

  it("closes the failed attempt and opens the one that took over", () => {
    const trail = buildAttemptTrail(META, [RESTART], "ok");

    expect(trail).toHaveLength(2);
    expect(trail[0]).toMatchObject({
      provider: "groq",
      outcome: "failed",
      // The normalized error code, rendered as prose. Provider-specific strings
      // die inside `parse_error` and never reach a browser.
      reason: "provider unavailable",
    });
    expect(trail[1]).toMatchObject({ attempt: 2, provider: "gemini", outcome: "served" });
  });

  it("marks the last attempt failed when the stream ran out of providers", () => {
    const trail = buildAttemptTrail(META, [RESTART], "failed");

    expect(trail[1]).toMatchObject({ outcome: "failed", reason: "no providers left to try" });
  });

  it("is empty before `meta` — there is nothing to report yet", () => {
    expect(buildAttemptTrail(null, [], "failed")).toEqual([]);
  });
});
