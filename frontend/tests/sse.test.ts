/**
 * The streaming client — framing, then transport.
 *
 * Two halves worth testing for different reasons. The parser is where a bug is
 * silent: a chunk boundary lands wherever TCP puts it, so a parser that works
 * against one tidy string and breaks against the same bytes split in two passes
 * every test and drops tokens in production. The transport is where D13 lives:
 * a pre-stream failure has to arrive as a `GatewayError` with its `request_id`
 * intact, exactly as the non-streaming path's failures always have.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { GatewayError, NetworkError } from "@/lib/api";
import { SseParser, openCompletionStream, type StreamEvent } from "@/lib/sse";

// The client needs a bearer token and nothing else from Supabase. Mocked at the
// module boundary so these tests need no environment and no session.
vi.mock("@/lib/supabase/client", () => ({
  getSupabaseBrowserClient: () => ({
    auth: { getSession: async () => ({ data: { session: { access_token: "test-token" } } }) },
  }),
}));

const META = {
  attempt: 1,
  slot: "general",
  provider: "groq",
  model: "llama-3.3-70b-versatile",
  requested_slot: "auto",
  conversation_id: "8b0d1f6e-0000-4000-8000-000000000000",
  message_id: "8b0d1f6e-0000-4000-8000-000000000001",
};

const DONE = {
  served_by: { slot: "general", provider: "groq", model: "llama-3.3-70b-versatile" },
  requested_slot: "auto",
  substituted: false,
  attempts: 1,
  usage: { prompt_tokens: 812, completion_tokens: 340, total_tokens: 1152, estimated: false },
  degraded: false,
  status: "ok" as const,
};

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

// --------------------------------------------------------------------------- //
// Framing
// --------------------------------------------------------------------------- //
describe("SseParser", () => {
  it("parses a full meta/delta/done sequence", () => {
    const parser = new SseParser();

    const events = parser.push(
      frame("meta", META) + frame("delta", { choices: [{ delta: { content: "Hel" } }] }) + frame("done", DONE),
    );

    expect(events.map((event) => event.event)).toEqual(["meta", "delta", "done"]);
    expect((events[0] as Extract<StreamEvent, { event: "meta" }>).data.model).toBe(
      "llama-3.3-70b-versatile",
    );
  });

  it("holds an incomplete frame until the rest of it arrives", () => {
    // The case that matters: a chunk boundary in the middle of a `data:` line.
    // A parser that ignores it looks correct in every single-chunk test and drops
    // tokens against a real socket.
    const parser = new SseParser();
    const whole = frame("delta", { choices: [{ delta: { content: "Hello" } }] });
    const split = Math.floor(whole.length / 2);

    expect(parser.push(whole.slice(0, split))).toEqual([]);

    const events = parser.push(whole.slice(split));
    expect(events).toHaveLength(1);
    expect((events[0] as Extract<StreamEvent, { event: "delta" }>).data.choices[0]?.delta.content).toBe(
      "Hello",
    );
  });

  it("keeps a trailing partial frame across many chunks", () => {
    const parser = new SseParser();
    const whole = frame("meta", META) + frame("delta", { choices: [{ delta: { content: "a" } }] });

    const seen: StreamEvent[] = [];
    for (const char of whole) seen.push(...parser.push(char));

    expect(seen.map((event) => event.event)).toEqual(["meta", "delta"]);
  });

  it("ignores heartbeat comments", () => {
    // `: heartbeat` frames arrive whenever the gateway is waiting on a slow
    // provider. Treating one as an event would render an empty delta every
    // fifteen seconds.
    const parser = new SseParser();

    expect(parser.push(": heartbeat\n\n: heartbeat\n\n")).toEqual([]);
    expect(parser.push(frame("done", DONE))).toHaveLength(1);
  });

  it("ignores an event type it does not know", () => {
    // Forward compatibility, same rule as the open end of `ContentBlock`: a fifth
    // event added by a later phase must degrade to "ignored", never to a broken
    // stream in a client that has not been redeployed.
    const parser = new SseParser();

    expect(parser.push(frame("quota_warning", { remaining: 3 }))).toEqual([]);
  });

  it("skips a frame whose data is not JSON rather than throwing", () => {
    const parser = new SseParser();

    expect(parser.push("event: delta\ndata: {not json\n\n")).toEqual([]);
    expect(parser.push(frame("done", DONE))).toHaveLength(1);
  });

  it("rejoins a multi-line data field", () => {
    const parser = new SseParser();
    const events = parser.push('event: delta\ndata: {"choices":[{"delta":\ndata: {"content":"x"}}]}\n\n');

    expect((events[0] as Extract<StreamEvent, { event: "delta" }>).data.choices[0]?.delta.content).toBe("x");
  });

  it("accepts CRLF line endings", () => {
    const parser = new SseParser();
    const events = parser.push(`event: done\r\ndata: ${JSON.stringify(DONE)}\r\n\r\n`);

    expect(events.map((event) => event.event)).toEqual(["done"]);
  });
});

// --------------------------------------------------------------------------- //
// Transport
// --------------------------------------------------------------------------- //
function streamingResponse(chunks: string[], init: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, ...init });
}

function stubFetch(response: Response | (() => Promise<Response>)) {
  // Typed as `fetch` itself so the recorded calls carry the real argument tuple.
  const impl: typeof fetch =
    typeof response === "function" ? async () => response() : async () => response;
  const fetchMock = vi.fn(impl);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("openCompletionStream", () => {
  it("dispatches events in order and asks for a stream", async () => {
    const fetchMock = stubFetch(
      streamingResponse([
        frame("meta", META),
        frame("delta", { choices: [{ delta: { content: "Hel" } }] }),
        ": heartbeat\n\n",
        frame("delta", { choices: [{ delta: { content: "lo" } }] }),
        frame("done", DONE),
      ]),
    );

    const seen: string[] = [];
    let text = "";

    await openCompletionStream(
      { model: "auto", messages: [{ role: "user", content: "hi" }] },
      {
        onMeta: () => seen.push("meta"),
        onDelta: (delta) => {
          seen.push("delta");
          text += delta.choices[0]?.delta.content ?? "";
        },
        onDone: () => seen.push("done"),
      },
    );

    expect(seen).toEqual(["meta", "delta", "delta", "done"]);
    expect(text).toBe("Hello");

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toMatchObject({ stream: true, model: "auto" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer test-token");
  });

  it("clears nothing itself — a restart is handed straight to the caller", async () => {
    stubFetch(
      streamingResponse([
        frame("meta", META),
        frame("delta", { choices: [{ delta: { content: "half an ans" } }] }),
        frame("restart", {
          reason: "provider_unavailable",
          failed: { provider: "groq", model: "llama-3.3-70b-versatile" },
          next: { slot: "general", provider: "gemini", model: "gemini-3.6-flash" },
          attempt: 2,
          discarded_chars: 11,
        }),
        frame("delta", { choices: [{ delta: { content: "a whole answer" } }] }),
        frame("done", { ...DONE, attempts: 2 }),
      ]),
    );

    const restarts: number[] = [];
    await openCompletionStream({ model: "auto", messages: [] }, {
      onRestart: (restart) => restarts.push(restart.discarded_chars),
    });

    expect(restarts).toEqual([11]);
  });

  it("throws the error envelope when the gateway fails before the first byte", async () => {
    // D13's payoff on the client: nothing was streamed, so the failure is an
    // ordinary JSON envelope and `GatewayError` handles it unchanged — including
    // the request_id a user can quote.
    stubFetch(
      new Response(
        JSON.stringify({
          error: {
            code: "provider_unavailable",
            message: "Every model provider is unavailable.",
            request_id: "01JABCDEF",
            details: {},
          },
        }),
        { status: 502 },
      ),
    );

    const error = await openCompletionStream({ model: "auto", messages: [] }, {}).catch((e) => e);

    expect(error).toBeInstanceOf(GatewayError);
    expect((error as GatewayError).code).toBe("provider_unavailable");
    expect((error as GatewayError).requestId).toBe("01JABCDEF");
  });

  it("rejects when the connection dies before `done`", async () => {
    // A truncated stream must not resolve as success — the caller would persist a
    // half answer and call it finished.
    stubFetch(
      streamingResponse([frame("meta", META), frame("delta", { choices: [{ delta: { content: "Hel" } }] })]),
    );

    const error = await openCompletionStream({ model: "auto", messages: [] }, {}).catch((e) => e);

    expect(error).toBeInstanceOf(NetworkError);
  });

  it("resolves quietly when the caller aborts", async () => {
    // Stopping a generation is a person changing their mind, not a failure, and a
    // UI that renders an error card for it is wrong about what happened.
    const controller = new AbortController();
    stubFetch(async () => {
      controller.abort();
      throw new DOMException("aborted", "AbortError");
    });

    await expect(
      openCompletionStream({ model: "auto", messages: [] }, {}, controller.signal),
    ).resolves.toBeUndefined();
  });

  it("reports an unreachable gateway as a transport failure", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });

    const error = await openCompletionStream({ model: "auto", messages: [] }, {}).catch((e) => e);

    expect(error).toBeInstanceOf(NetworkError);
  });
});
