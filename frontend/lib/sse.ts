/**
 * The streaming client — §1.1's wire protocol, read in the browser.
 *
 * The four event shapes below were transcribed from the contract in Phase 1 and
 * have not changed since; `app/streaming/sse.py` is their server-side twin, and a
 * JSON diff between the two files is the tell that one of them drifted. Two
 * properties they were designed for, both now load-bearing:
 *
 * 1. `DoneEvent` is structurally the non-streaming response's provenance block,
 *    so `provenance.ts` gained exactly one function (`fromDoneEvent`) and
 *    `ModelIndicator` gained nothing at all.
 * 2. `RestartEvent` carries the client-side contract: on `restart`, clear the
 *    in-progress bubble entirely, swap the indicator, resume appending. Never
 *    splice two attempts together — half an answer from one model welded to a
 *    full answer from another reads as a broken *model*, and someone will spend
 *    a day blaming the provider for a client bug.
 *
 * **`EventSource` cannot do this.** It is GET-only and cannot set an
 * `Authorization` header, so the transport is `fetch` plus a reader plus manual
 * frame parsing. That is also why the parser is a separate exported class: the
 * framing rules are worth testing on their own, without a network or a session.
 */

import { GATEWAY_BASE, NetworkError, authHeaders, gatewayErrorFrom } from "./api";
import type { ChatCompletionRequest, ExtractionTier, ServedBy, UsageOut } from "./types";

export type MetaEvent = {
  attempt: number;
  slot: string;
  provider: string;
  model: string;
  requested_slot: string;
  conversation_id: string;
  message_id: string;
};

export type DeltaEvent = {
  choices: { delta: { content?: string } }[];
};

export type RestartEvent = {
  reason: string;
  failed: { provider: string; model: string };
  next: { slot: string; provider: string; model: string };
  attempt: number;
  discarded_chars: number;
};

export type DoneEvent = {
  served_by: ServedBy;
  requested_slot: string;
  substituted: boolean;
  attempts: number;
  usage: UsageOut & { wasted_tokens_out?: number };
  degraded: boolean;
  /** How the turn's attachments reached the model (Phase 4): the worst tier of
   * `cache` | `native` | `llm` | `local`, or null when it carried none. */
  extraction_tier?: ExtractionTier | null;
  /** D34's streaming twin of the completion response's own field: how many
   *  older turns D4's fitting dropped to build this answer. */
  messages_dropped?: number;
  /** D32's streaming twin: the pin disclosure. Rides on a successful stream —
   *  it is not an error, and must not be rendered as one (trap 6). */
  warning?: string | null;
  status: "ok" | "failed";
  /** Present only on `status: "failed"` with a non-trivial partial buffer. */
  partial_content?: string;
};

export type StreamEvent =
  | { event: "meta"; data: MetaEvent }
  | { event: "delta"; data: DeltaEvent }
  | { event: "restart"; data: RestartEvent }
  | { event: "done"; data: DoneEvent };

export type StreamHandlers = {
  onMeta?: (event: MetaEvent) => void;
  onDelta?: (event: DeltaEvent) => void;
  /** Clear the bubble. Do not diff, do not splice. */
  onRestart?: (event: RestartEvent) => void;
  onDone?: (event: DoneEvent) => void;
};

// --------------------------------------------------------------------------- //
// Framing
// --------------------------------------------------------------------------- //
const EVENT_NAMES = new Set(["meta", "delta", "restart", "done"]);

/**
 * Bytes off the wire → whole events.
 *
 * Stateful across calls on purpose: a chunk boundary lands wherever TCP puts it,
 * which is routinely in the middle of a `data:` line, so the parser holds the
 * incomplete tail until the rest arrives. That case is the one most likely to be
 * "working" in a test that feeds it one tidy string and broken in production.
 *
 * Two things it declines to do, both deliberately:
 *
 * - **A comment line is not an event.** `: heartbeat` frames arrive whenever the
 *   gateway is waiting on a slow provider, and a client that mistook one for a
 *   real event would render an empty delta every fifteen seconds.
 * - **An unrecognised event name is skipped, not thrown.** Same reasoning as the
 *   open end of `ContentBlock` in `types.ts`: a fifth event type added by a later
 *   phase must degrade to "ignored", never to a broken stream in an old client.
 */
export class SseParser {
  private buffer = "";

  /** Feed a decoded chunk; get back whatever events completed with it. */
  push(chunk: string): StreamEvent[] {
    // Normalized so the frame split has one delimiter to look for rather than
    // three. SSE permits CR, LF and CRLF line endings interchangeably.
    this.buffer += chunk.replace(/\r\n?/g, "\n");

    const events: StreamEvent[] = [];
    let boundary = this.buffer.indexOf("\n\n");

    while (boundary !== -1) {
      const frame = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);

      const event = parseFrame(frame);
      if (event) events.push(event);

      boundary = this.buffer.indexOf("\n\n");
    }

    return events;
  }
}

function parseFrame(frame: string): StreamEvent | null {
  let name = "";
  const data: string[] = [];

  for (const line of frame.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // heartbeats and blanks

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // One optional space after the colon is part of the framing, not the value.
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");

    if (field === "event") name = value;
    else if (field === "data") data.push(value);
  }

  if (!EVENT_NAMES.has(name) || data.length === 0) return null;

  try {
    // Multi-line `data:` rejoins with newlines — the spec's rule, and free
    // insurance against a provider's text ever being framed across lines.
    return { event: name, data: JSON.parse(data.join("\n")) } as StreamEvent;
  } catch {
    return null;
  }
}

// --------------------------------------------------------------------------- //
// The transport
// --------------------------------------------------------------------------- //
/**
 * Stream one turn, dispatching each event as it lands.
 *
 * Resolves when the response body is exhausted — which, usefully, is *after* the
 * gateway's collector has finished persisting: the orchestrator writes the
 * `messages` row after its final `yield`, so the socket does not close until that
 * is done. A caller revalidating its transcript cache on this promise therefore
 * has no race with the write.
 *
 * Rejects with:
 *
 * - `GatewayError` — a pre-stream failure. Not a special case: the gateway
 *   commits to a 200 only once a candidate has produced its first chunk (D13),
 *   so a fully-down provider pool arrives here as the ordinary error envelope,
 *   `request_id` and all. A mid-stream failure is *not* this; it arrives in-band
 *   as `done` with `status: "failed"`, because by then the status line is spent.
 * - `NetworkError` — the gateway was never reached, or the connection died
 *   before `done`. A truncated stream must not resolve as success.
 *
 * An abort is neither: a cancelled turn is a person changing their mind, and it
 * resolves quietly.
 */
export async function openCompletionStream(
  body: Omit<ChatCompletionRequest, "stream">,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${GATEWAY_BASE}/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify({ ...body, stream: true }),
      signal,
    });
  } catch (error) {
    if (isAbort(error)) return;
    throw new NetworkError();
  }

  if (!response.ok) throw await gatewayErrorFrom(response);
  if (!response.body) throw new NetworkError("The gateway returned no response body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  let sawDone = false;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      for (const event of parser.push(decoder.decode(value, { stream: true }))) {
        if (dispatch(event, handlers)) sawDone = true;
      }
    }
  } catch (error) {
    if (isAbort(error)) return;
    throw new NetworkError();
  } finally {
    // Releases the connection. Without it an aborted turn leaves the gateway
    // streaming into a socket nobody is reading, which is exactly the upstream
    // generation the Stop button is meant to end.
    void reader.cancel().catch(() => {});
  }

  if (!sawDone && !signal?.aborted) {
    throw new NetworkError("The connection closed before the model finished.");
  }
}

/** Route one event to its handler. Returns true for the terminal one. */
function dispatch(event: StreamEvent, handlers: StreamHandlers): boolean {
  switch (event.event) {
    case "meta":
      handlers.onMeta?.(event.data);
      return false;
    case "delta":
      handlers.onDelta?.(event.data);
      return false;
    case "restart":
      handlers.onRestart?.(event.data);
      return false;
    case "done":
      handlers.onDone?.(event.data);
      return true;
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
