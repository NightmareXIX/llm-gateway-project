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
  servedBy: { slot: "general", provider: "groq", model: "openai/gpt-oss-120b" },
  requestedSlot: "auto",
  substituted: false,
  attempts: 1,
  degraded: false,
  extractionTier: null,
  messagesDropped: 0,
  warning: null,
  keyPool: "shared",
  tokensIn: 812,
  tokensOut: 340,
  wastedTokensOut: 0,
};

describe("rule 1 — served_by is always rendered", () => {
  it("names the model, the provider and the slot", () => {
    render(<ModelIndicator provenance={BASE} />);

    expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument();
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
    expect(screen.queryByText(/omitted/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pinned/)).not.toBeInTheDocument();
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
              model: "openai/gpt-oss-120b",
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

describe("rule 4 — degraded says so, and says why", () => {
  it("falls back to a bare degraded marker on a row with no tier recorded", () => {
    // Every assistant row written before Phase 4. All that was recorded is that
    // something went wrong, and inventing a tier for it would be worse than the
    // vaguer sentence.
    render(<ModelIndicator provenance={{ ...BASE, degraded: true }} />);

    expect(screen.getByText("read with local extraction")).toBeInTheDocument();
  });

  it("names local OCR as the reader when that is what read the document", () => {
    render(
      <ModelIndicator provenance={{ ...BASE, degraded: true, extractionTier: "local" }} />,
    );

    expect(screen.getByText("read by local OCR")).toBeInTheDocument();
    // The full sentence is available without a pointer — the reason a person
    // should distrust this answer is information, not a hover affordance.
    expect(screen.getAllByText(/ran OCR over it/).length).toBeGreaterThan(0);
  });

  it("discloses a tier even when the answer was not degraded", () => {
    // `served_by`'s discipline, applied to perception: "read directly" and
    // "read by another model" are different guarantees, and the gateway does
    // not get to be silent about which one this answer got.
    render(<ModelIndicator provenance={{ ...BASE, extractionTier: "native" }} />);

    expect(screen.getByText("read directly")).toBeInTheDocument();
    expect(screen.getAllByText(/GPT-OSS 120B was handed the file itself/).length).toBe(1);
  });

  it("says an extraction was reused rather than pretending it was fresh", () => {
    render(<ModelIndicator provenance={{ ...BASE, extractionTier: "cache" }} />);

    expect(screen.getByText("read earlier")).toBeInTheDocument();
  });

  it("says nothing about perception for a turn that carried no attachment", () => {
    render(<ModelIndicator provenance={BASE} />);

    expect(screen.queryByText(/read /)).not.toBeInTheDocument();
  });
});

describe("rule 5 — truncation says how much was dropped", () => {
  it("names the number of omitted messages, not just that some were", () => {
    // D34's whole argument in one assertion: an integer on the wire because
    // "148 earlier messages omitted" is a sentence worth reading and "some
    // earlier messages were omitted" is not.
    render(<ModelIndicator provenance={{ ...BASE, messagesDropped: 148 }} />);

    expect(screen.getByText("148 earlier messages omitted")).toBeInTheDocument();
  });

  it("says nothing when the whole history reached the model", () => {
    render(<ModelIndicator provenance={{ ...BASE, messagesDropped: 0 }} />);

    expect(screen.queryByText(/omitted/)).not.toBeInTheDocument();
  });

  it("does not say '1 earlier messages'", () => {
    render(<ModelIndicator provenance={{ ...BASE, messagesDropped: 1 }} />);

    expect(screen.getByText("1 earlier message omitted")).toBeInTheDocument();
  });

  it("discloses truncation on a turn that is otherwise perfectly ordinary", () => {
    // The case that matters: nothing failed, nothing was substituted, no
    // attachment was involved — the answer just wasn't built on the whole
    // thread, and that is the only thing wrong with it.
    render(<ModelIndicator provenance={{ ...BASE, messagesDropped: 12 }} />);

    expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument();
    expect(screen.getByText("12 earlier messages omitted")).toBeInTheDocument();
    expect(screen.queryByText(/was unavailable/)).not.toBeInTheDocument();
  });
});

describe("rule 6 — a pinned conversation discloses its pin", () => {
  const WARNING = "conversation pinned to gemini/gemini-3.6-flash due to prior tool use";

  it("renders the gateway's own wording verbatim", () => {
    // Deliberately not reworded here. The server builds this string in one
    // place (`selection.pin_warning`) precisely so the model name in it is the
    // real one; a client that paraphrased would have to know the pin's shape.
    render(<ModelIndicator provenance={{ ...BASE, warning: WARNING }} />);

    expect(screen.getByText(WARNING)).toBeInTheDocument();
  });

  it("says nothing on an unpinned turn", () => {
    render(<ModelIndicator provenance={{ ...BASE, warning: null }} />);

    expect(screen.queryByText(/pinned/)).not.toBeInTheDocument();
  });

  it("is a disclosure on a real answer, not an error (trap 6)", () => {
    // The indicator still renders everything it renders for a normal turn.
    // A pinned answer is a *served* answer; the warning sits beside the
    // provenance, in the same register as the degraded notice.
    render(<ModelIndicator provenance={{ ...BASE, warning: WARNING }} />);

    expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument();
    expect(screen.getByText("Groq")).toBeInTheDocument();
    expect(screen.getByText(WARNING)).toBeInTheDocument();
  });
});

describe("rule 7 — a private key is disclosed, the shared pool is not", () => {
  it("says whose key served the answer when it was the user's own", () => {
    render(<ModelIndicator provenance={{ ...BASE, keyPool: "private" }} />);

    expect(screen.getByText("your Groq key")).toBeInTheDocument();
  });

  it("stays silent on the shared pool", () => {
    // The default every account is on. A badge on every message announcing it
    // would be noise, and noise is what makes real disclosures unreadable.
    render(<ModelIndicator provenance={{ ...BASE, keyPool: "shared" }} />);

    expect(screen.queryByText(/your .* key/)).not.toBeInTheDocument();
  });

  it("stays silent when nothing was spent at all", () => {
    // A cache hit, or a mid-stream `meta`. There is no pool to name.
    render(<ModelIndicator provenance={{ ...BASE, keyPool: null }} />);

    expect(screen.queryByText(/your .* key/)).not.toBeInTheDocument();
  });

  it("is a disclosure on a real answer, not an error", () => {
    render(<ModelIndicator provenance={{ ...BASE, keyPool: "private" }} />);

    expect(screen.getByText("GPT-OSS 120B")).toBeInTheDocument();
    expect(screen.queryByText(/was unavailable/)).not.toBeInTheDocument();
    // The billing consequence is the part worth spelling out, and it is
    // available without a pointer.
    expect(screen.getAllByText(/billed to your Groq account/).length).toBeGreaterThan(0);
  });
});

describe("provenance adapters", () => {
  const meta: MessageMeta = {
    provider_used: "groq",
    model_used: "openai/gpt-oss-120b",
    slot_used: "general",
    requested_slot: "auto",
    substituted: false,
    attempts: 1,
    tokens_in: 812,
    tokens_out: 340,
    wasted_tokens_out: 0,
    degraded: false,
    extraction_tier: null,
    messages_dropped: 0,
    key_pool: "shared",
  };

  it("builds provenance from a stored message's meta", () => {
    expect(fromMessageMeta(meta)).toEqual(BASE);
  });

  it("carries the extraction tier off a stored row", () => {
    expect(fromMessageMeta({ ...meta, extraction_tier: "llm" })?.extractionTier).toBe("llm");
  });

  it("carries a stored truncation count off the row", () => {
    expect(fromMessageMeta({ ...meta, messages_dropped: 148 })?.messagesDropped).toBe(148);
  });

  it("reads a pre-Phase-5 row's missing truncation count as 0, not as unknown", () => {
    // Trap 7: nobody knows whether a row written before the field existed was
    // truncated, and there is no backfill. The default is the honest reading.
    const withoutCount: Partial<MessageMeta> = { ...meta };
    delete withoutCount.messages_dropped;

    expect(fromMessageMeta(withoutCount)?.messagesDropped).toBe(0);
  });

  it("carries the key pool off a stored row", () => {
    // Unlike `warning`, this one *is* stored: which credential paid for a turn
    // is a fact about the turn, so a reopened thread still discloses it.
    expect(fromMessageMeta({ ...meta, key_pool: "private" })?.keyPool).toBe("private");
  });

  it("reads a pre-Phase-6 row's missing key pool as null, not as shared", () => {
    const withoutPool: Partial<MessageMeta> = { ...meta };
    delete withoutPool.key_pool;

    expect(fromMessageMeta(withoutPool)?.keyPool).toBeNull();
  });

  it("never reads a pin warning off a stored row", () => {
    // The warning is about *this request* — which slot it asked for, and what
    // the pin did to that. A stored row has no request to disclose against, so
    // there is no key to read and nothing to invent.
    expect(fromMessageMeta(meta)?.warning).toBeNull();
  });

  it("reads a pre-Phase-4 row's missing tier as no attachment, not as unknown", () => {
    const withoutTier: Partial<MessageMeta> = { ...meta };
    delete withoutTier.extraction_tier;

    expect(fromMessageMeta(withoutTier)?.extractionTier).toBeNull();
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
      model: "openai/gpt-oss-120b",
      choices: [{ index: 0, message: { role: "assistant", content: "hi" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 812, completion_tokens: 340, total_tokens: 1152, estimated: false },
      served_by: { slot: "general", provider: "groq", model: "openai/gpt-oss-120b" },
      requested_slot: "auto",
      substituted: false,
      attempts: 1,
      degraded: false,
      key_pool: "shared",
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
    model: "openai/gpt-oss-20b",
    requested_slot: "fast",
    conversation_id: "8b0d1f6e-0000-4000-8000-000000000000",
    message_id: "8b0d1f6e-0000-4000-8000-000000000001",
  };

  const restart: RestartEvent = {
    reason: "provider_unavailable",
    failed: { provider: "groq", model: "openai/gpt-oss-20b" },
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

  it("carries the tier off `done` onto the same indicator", () => {
    // The streaming path's own copy of the disclosure. It reaches the component
    // through `provenance.ts` exactly as the stored row's does, which is the
    // check that the Phase 1 contract is still doing its job.
    render(
      <ModelIndicator provenance={fromDoneEvent({ ...done, extraction_tier: "llm" })} />,
    );

    expect(screen.getByText("read by another model")).toBeInTheDocument();
  });

  it("carries the truncation count and the pin warning off `done` too", () => {
    // Trap 8's client-side half: `DoneEvent` gained two fields, and a streamed
    // answer must disclose exactly what the non-streaming one does. If this
    // ever renders less than `fromCompletion`'s twin, the streaming path is
    // quietly the less honest of the two.
    render(
      <ModelIndicator
        provenance={fromDoneEvent({
          ...done,
          messages_dropped: 148,
          warning: "conversation pinned to gemini/gemini-3.6-flash due to prior tool use",
        })}
      />,
    );

    expect(screen.getByText("148 earlier messages omitted")).toBeInTheDocument();
    expect(
      screen.getByText("conversation pinned to gemini/gemini-3.6-flash due to prior tool use"),
    ).toBeInTheDocument();
  });
});
