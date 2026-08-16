/**
 * The `ModelIndicator` contract, pinned.
 *
 * This component's rendering rules are frozen — §1.1 specifies them, Phase 2
 * exercises the branches Phase 1 cannot reach, and the whole reason for
 * building it now is that later phases must not need to change it. A test is
 * the only thing that makes "frozen" mean something.
 *
 * Every case here is written against the *spec*, not against the current
 * implementation's markup: what must appear on screen, not which element it
 * lives in.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ModelIndicator } from "@/components/ModelIndicator";
import {
  buildAttemptTrail,
  fromCompletion,
  fromDoneEvent,
  fromMessageMeta,
  type Provenance,
} from "@/lib/provenance";
import type { DoneEvent, MetaEvent, RestartEvent } from "@/lib/sse";
import type { ChatCompletionResponse, MessageMeta } from "@/lib/types";

const BASE: Provenance = {
  servedBy: { slot: "general", provider: "groq", model: "llama-3.3-70b-versatile" },
  requestedSlot: "auto",
  substituted: false,
  attempts: 1,
  degraded: false,
  tokensIn: 812,
  tokensOut: 340,
  wastedTokensOut: 0,
};

describe("rule 1 — served_by is always rendered", () => {
  it("names the model, the provider and the slot", () => {
    render(<ModelIndicator provenance={BASE} />);

    expect(screen.getByText("Llama 3.3 70B")).toBeInTheDocument();
    expect(screen.getByText("Groq")).toBeInTheDocument();
    expect(screen.getByText("general")).toBeInTheDocument();
  });

  it("falls back to the wire model name when the model is unknown", () => {
    // A gateway that discloses provenance must never prettify away a model it
    // does not recognise — a new entry in providers.yaml has to show up as
    // itself, not as "Unknown model".
    render(
      <ModelIndicator
        provenance={{
          ...BASE,
          servedBy: { slot: "llm3", provider: "openrouter", model: "deepseek-r1:free" },
        }}
      />,
    );

    expect(screen.getByText("deepseek-r1:free")).toBeInTheDocument();
  });

  it("renders in the plain Phase 1 case without any state markers", () => {
    render(<ModelIndicator provenance={BASE} />);

    expect(screen.queryByText(/was unavailable/)).not.toBeInTheDocument();
    expect(screen.queryByText(/attempts/)).not.toBeInTheDocument();
    expect(screen.queryByText(/local extraction/)).not.toBeInTheDocument();
  });
});

describe("rule 2 — substitution discloses the mismatch", () => {
  it("names the slot that was asked for and could not serve", () => {
    render(
      <ModelIndicator
        provenance={{
          ...BASE,
          servedBy: { slot: "llm1", provider: "gemini", model: "gemini-flash" },
          requestedSlot: "llm2",
          substituted: true,
        }}
      />,
    );

    expect(screen.getByText("gemini-flash")).toBeInTheDocument();
    expect(screen.getByText("llm2 was unavailable")).toBeInTheDocument();
  });
});

describe("rule 3 — attempts > 1 carries the trail", () => {
  it("shows the attempt count", () => {
    render(<ModelIndicator provenance={{ ...BASE, attempts: 2, wastedTokensOut: 96 }} />);

    expect(screen.getByText("2 attempts")).toBeInTheDocument();
  });

  it("exposes the trail to assistive technology, not only on hover", () => {
    render(
      <ModelIndicator
        provenance={{
          ...BASE,
          attempts: 2,
          attemptTrail: [
            {
              attempt: 1,
              provider: "groq",
              model: "llama-3.3-70b-versatile",
              slot: "general",
              outcome: "failed",
              reason: "provider unavailable",
            },
            {
              attempt: 2,
              provider: "gemini",
              model: "gemini-flash",
              slot: "llm1",
              outcome: "served",
            },
          ],
        }}
      />,
    );

    // Present in the DOM without a pointer event: the trail is information, and
    // a tooltip alone would make it mouse-only.
    expect(screen.getByText(/provider unavailable/)).toBeInTheDocument();
  });
});

describe("rule 4 — degraded says so", () => {
  it("marks an answer that reached the model through local extraction", () => {
    render(<ModelIndicator provenance={{ ...BASE, degraded: true }} />);

    expect(screen.getByText("read with local extraction")).toBeInTheDocument();
  });
});

describe("provenance adapters", () => {
  const meta: MessageMeta = {
    provider_used: "groq",
    model_used: "llama-3.3-70b-versatile",
    slot_used: "general",
    requested_slot: "auto",
    substituted: false,
    attempts: 1,
    tokens_in: 812,
    tokens_out: 340,
    wasted_tokens_out: 0,
    degraded: false,
    extraction_tier: null,
  };

  it("builds provenance from a stored message's meta", () => {
    expect(fromMessageMeta(meta)).toEqual(BASE);
  });

  it("returns null when a row carries no provider", () => {
    // Invariant 5: provider_used is non-null on every assistant message. A row
    // without one is not an assistant turn, and inventing "unknown model" for
    // it would be a worse answer than rendering nothing.
    expect(fromMessageMeta({})).toBeNull();
    expect(fromMessageMeta(undefined)).toBeNull();
  });

  it("builds the same provenance from a fresh completion response", () => {
    const response: ChatCompletionResponse = {
      id: "01JABCDEF",
      object: "chat.completion",
      created: 1_760_000_000,
      model: "llama-3.3-70b-versatile",
      choices: [{ index: 0, message: { role: "assistant", content: "hi" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 812, completion_tokens: 340, total_tokens: 1152, estimated: false },
      served_by: { slot: "general", provider: "groq", model: "llama-3.3-70b-versatile" },
      requested_slot: "auto",
      substituted: false,
      attempts: 1,
      degraded: false,
      conversation_id: "8b0d1f6e-0000-4000-8000-000000000000",
      message_id: "8b0d1f6e-0000-4000-8000-000000000001",
    };

    // The two sources must agree: a message rendered from the response and the
    // same message rendered from history after a refresh have to look identical.
    expect(fromCompletion(response)).toEqual(fromMessageMeta(meta));
  });
});

describe("a restarted stream, end to end", () => {
  // The case the whole phase exists for, rendered the way a user sees it: Groq
  // died mid-sentence, Gemini finished the answer, and the chip has to say so.
  // Nothing in `ModelIndicator` was changed to make this work — the events go
  // through `provenance.ts` and come out as the props it has always taken, which
  // is the check that the Phase 1 contract really was frozen.
  const meta: MetaEvent = {
    attempt: 1,
    slot: "fast",
    provider: "groq",
    model: "llama-3.1-8b-instant",
    requested_slot: "fast",
    conversation_id: "8b0d1f6e-0000-4000-8000-000000000000",
    message_id: "8b0d1f6e-0000-4000-8000-000000000001",
  };

  const restart: RestartEvent = {
    reason: "provider_unavailable",
    failed: { provider: "groq", model: "llama-3.1-8b-instant" },
    next: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
    attempt: 2,
    discarded_chars: 412,
  };

  const done: DoneEvent = {
    served_by: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
    requested_slot: "fast",
    substituted: true,
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

  it("names the model that finished, the slot that failed, and the trail", () => {
    render(
      <ModelIndicator
        provenance={fromDoneEvent(done, buildAttemptTrail(meta, [restart], "ok"))}
      />,
    );

    expect(screen.getByText("Gemini 3.6 Flash")).toBeInTheDocument();
    expect(screen.getByText("fast was unavailable")).toBeInTheDocument();
    expect(screen.getByText("2 attempts")).toBeInTheDocument();
    // The trail, without a pointer: the attempt that died and why.
    expect(screen.getByText(/provider unavailable/)).toBeInTheDocument();
  });
});
