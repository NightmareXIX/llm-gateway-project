/**
 * Streaming — the Phase 2 seam, declared now, unimplemented on purpose.
 *
 * The repo's rule for deferred work is "a typed signature, never a silently
 * passing stub". This is that rule applied on the frontend: the four event
 * shapes below are §1.1's wire protocol transcribed exactly, and
 * `openCompletionStream` throws rather than pretending.
 *
 * Two things this file buys today, before a byte of it runs:
 *
 * 1. `DoneEvent` is structurally the non-streaming response's provenance block.
 *    That is the check that `ModelIndicator`'s props already cover the streamed
 *    case — Phase 2 adds `fromDoneEvent` to `provenance.ts` and nothing else.
 * 2. `RestartEvent` records the client-side contract in the one place a future
 *    implementer will look: on `restart`, clear the in-progress bubble entirely,
 *    swap the indicator, resume appending. Never splice two attempts together.
 */

import type { ServedBy, UsageOut } from "./types";

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

export function openCompletionStream(
  _body: unknown,
  _handlers: StreamHandlers,
  _signal?: AbortSignal,
): Promise<void> {
  throw new Error(
    "Streaming lands in Phase 2. The gateway rejects `stream: true` with a 400 today.",
  );
}
