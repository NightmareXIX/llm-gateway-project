"use client";

import { useCallback, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR, { mutate as globalMutate } from "swr";

import { GatewayError, NetworkError, api, swrFetcher } from "./api";
import { deriveTitle } from "./format";
import { buildAttemptTrail, fromDoneEvent, fromMetaEvent, type Provenance } from "./provenance";
import { openCompletionStream, type DoneEvent, type MetaEvent, type RestartEvent } from "./sse";
import type { Conversation, ConversationDetail, Me, Message, MessageMeta } from "./types";

export const CONVERSATIONS_KEY = "/v1/conversations";
export const conversationKey = (id: string) => `/v1/conversations/${id}`;

/** The slot every Phase 1 request asks for. `auto` lets the gateway choose —
 *  which is also why `substituted` can only ever be false today. A picker
 *  replaces this constant in a later phase, once `/v1/models` exists. */
export const DEFAULT_SLOT = "auto";

export function useMe() {
  const { data, error, isLoading } = useSWR<Me>("/v1/me", swrFetcher, {
    revalidateOnFocus: false,
  });
  return { me: data, error, isLoading };
}

export function useConversations() {
  const { data, error, isLoading, mutate } = useSWR<Conversation[]>(
    CONVERSATIONS_KEY,
    swrFetcher,
  );
  return { conversations: data, error, isLoading, mutate };
}

export function useConversation(id: string | null) {
  const { data, error, isLoading, mutate } = useSWR<ConversationDetail>(
    id ? conversationKey(id) : null,
    swrFetcher,
    // A transcript is immutable history plus whatever this tab just sent. Focus
    // revalidation would re-fetch every time the user tabs back for no new data.
    { revalidateOnFocus: false },
  );
  return { conversation: data, error, isLoading, mutate };
}

/**
 * The turn currently in flight, and everything on screen because of it.
 *
 * The four statuses are four different things to render, not four flavours of
 * "busy":
 *
 * - `sending`   — the request is out, nothing has come back. Thinking dots. The
 *                 gateway sends no byte at all until a provider produces its
 *                 first token (D13), so this state is real and can last seconds.
 * - `streaming` — `meta` has landed and deltas are arriving. A live bubble.
 * - `unsaved`   — the user stopped it, or kept a failed stream's partial. There
 *                 is text on screen and no row behind it, and saying so is the
 *                 honest option.
 * - `failed`    — no answer. Error card, with the partial offered if there is one.
 */
export type PendingTurn = {
  text: string;
  status: "sending" | "streaming" | "unsaved" | "failed";
  /**
   * The **current attempt's** text. Cleared on every `restart`, never appended
   * across one: splicing half an answer from one model onto a full answer from
   * another reads as a broken model rather than as a client bug (§1.1).
   */
  answer: string;
  /** Who is answering *right now* — from `meta`, swapped on `restart`, finalized by `done`. */
  provenance: Provenance | null;
  /** Every restart this turn has been through. Drives the notice and the trail. */
  restarts: RestartEvent[];
  /** A failed stream's salvageable text, until the user keeps or discards it. */
  partial?: string;
  error?: GatewayError | NetworkError;
};

/**
 * The send path: a live streamed turn, an optimistic transcript, honest failure.
 *
 * Streaming is not optional here — the UI always asks for it. The non-streaming
 * path stays alive in `api.createCompletion` as the documented fallback, but a
 * second code path in the component tree would be a second place for the
 * restart contract to be got wrong.
 *
 * The ordering that makes this work is a property of the gateway, not luck: the
 * collector persists *after* the final frame is yielded, so the response body
 * does not close until the row is written. Revalidating when the stream promise
 * resolves therefore reads the truth rather than racing it.
 */
export function useSendMessage(conversationId: string | null) {
  const router = useRouter();
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Held outside React state because the handlers need to *read* them, and a
  // state setter's argument is the only place a component can see the value it
  // just queued. State is what gets rendered; these are what gets assembled.
  const metaRef = useRef<MetaEvent | null>(null);
  const doneRef = useRef<DoneEvent | null>(null);
  const answerRef = useRef("");

  /**
   * Clear the previous turn's accumulators.
   *
   * A function rather than three inline assignments so that TypeScript's flow
   * analysis does not narrow every later read of these refs to the `null` it can
   * see being written — the handlers' writes happen in callbacks it cannot follow.
   */
  const resetTurn = useCallback(() => {
    metaRef.current = null;
    doneRef.current = null;
    answerRef.current = "";
  }, []);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      const controller = new AbortController();
      abortRef.current = controller;
      resetTurn();

      setPending({
        text: trimmed,
        status: "sending",
        answer: "",
        provenance: null,
        restarts: [],
      });

      try {
        await openCompletionStream(
          {
            model: DEFAULT_SLOT,
            messages: [{ role: "user", content: trimmed }],
            ...(conversationId ? { conversation_id: conversationId } : {}),
          },
          {
            onMeta: (meta) => {
              metaRef.current = meta;
              setPending((current) =>
                current && { ...current, status: "streaming", provenance: fromMetaEvent(meta) },
              );
            },

            onDelta: (delta) => {
              const chunk = delta.choices[0]?.delta.content;
              if (!chunk) return;
              answerRef.current += chunk;
              setPending((current) => current && { ...current, answer: answerRef.current });
            },

            onRestart: (restart) => {
              // The contract, in one assignment: `answer: ""`. Not a diff, not a
              // splice, not a "keep the longer one" heuristic.
              answerRef.current = "";
              setPending((current) => {
                if (!current) return current;
                const restarts = [...current.restarts, restart];
                return {
                  ...current,
                  answer: "",
                  restarts,
                  // The indicator swaps with the bubble, not after it. Both halves
                  // of "something else is answering now" have to move together or
                  // the disclosure is briefly wrong.
                  provenance: current.provenance && {
                    ...current.provenance,
                    servedBy: restart.next,
                    attempts: restart.attempt,
                    attemptTrail: buildAttemptTrail(metaRef.current, restarts, "ok"),
                  },
                };
              });
            },

            onDone: (done) => {
              doneRef.current = done;
            },
          },
          controller.signal,
        );
      } catch (error) {
        // Two things reach here, and neither is a provider giving up mid-answer:
        // a pre-stream failure, which D13 keeps as an ordinary error envelope,
        // and a connection that died before `done`. A *provider* failure with the
        // stream already open arrives in-band instead, as `done` with
        // `status: "failed"`, and is handled below.
        //
        // The half-written answer goes with it. It was never stored, no model
        // finished it, and leaving it under an error card would present an
        // abandoned fragment as a result.
        setPending((current) =>
          current
            ? {
                ...current,
                status: "failed",
                answer: "",
                error: error instanceof Error ? (error as GatewayError | NetworkError) : undefined,
              }
            : current,
        );

        // The user's message did land server-side unless the request never
        // arrived. Re-read rather than guess.
        if (conversationId && !(error instanceof NetworkError)) {
          void globalMutate(conversationKey(conversationId));
        }
        return;
      } finally {
        abortRef.current = null;
      }

      const meta = metaRef.current;
      const done = doneRef.current;

      if (controller.signal.aborted) {
        // Not a failure. The gateway saw the disconnect, stopped the upstream
        // generation and persisted nothing, so what is on screen is all there is.
        //
        // Deliberately no revalidation: the stored history holds the user's turn
        // and no answer, and pulling it in now would render that turn twice —
        // once stored, once as this pending echo. The next navigation or refresh
        // reconciles it, and losing the text then is precisely what "not saved"
        // was telling the user would happen.
        setPending((current) => current && { ...current, status: "unsaved" });
        return;
      }

      if (!meta || !done || done.status === "failed") {
        setPending(
          (current) =>
            current && {
              ...current,
              status: "failed",
              answer: "",
              partial: done?.partial_content,
              provenance:
                done && meta
                  ? fromDoneEvent(done, buildAttemptTrail(meta, current.restarts, "failed"))
                  : current.provenance,
            },
        );
        // A failed stream writes no `messages` row — only a `requests` one — so
        // revalidating restores the transcript to exactly the user turn the
        // endpoint committed before any of this started.
        const id = conversationId ?? meta?.conversation_id;
        if (id) void globalMutate(conversationKey(id));
        return;
      }

      const isNewConversation = conversationId === null;
      const id = meta.conversation_id;

      await applyOptimisticTurn({
        conversationId: id,
        messageId: meta.message_id,
        userText: trimmed,
        answer: answerRef.current,
        done,
      });
      setPending(null);

      if (isNewConversation) {
        // Client-derived title. The gateway generates none, so without this every
        // sidebar row reads "New conversation". Deliberately not awaited on the
        // critical path — a failed rename must not look like a failed message.
        void api
          .renameConversation(id, deriveTitle(trimmed))
          .then(() => globalMutate(CONVERSATIONS_KEY))
          .catch(() => globalMutate(CONVERSATIONS_KEY));

        router.replace(`/chat/${id}`);
      } else {
        void globalMutate(CONVERSATIONS_KEY);
      }
    },
    [conversationId, router, resetTurn],
  );

  /** Abort the in-flight stream. Not an error — a person changing their mind. */
  const stop = useCallback(() => abortRef.current?.abort(), []);

  const retry = useCallback(() => {
    if (pending?.status === "failed") void send(pending.text);
  }, [pending, send]);

  /** Pin a failed stream's partial into the transcript, unsaved and labelled so. */
  const keepPartial = useCallback(() => {
    setPending((current) =>
      current?.partial
        ? { ...current, status: "unsaved", answer: current.partial, partial: undefined }
        : current,
    );
  }, []);

  const dismiss = useCallback(() => {
    abortRef.current?.abort();
    setPending(null);
  }, []);

  return { pending, send, stop, retry, keepPartial, dismiss };
}

/**
 * Seed the transcript cache with the turn that just completed.
 *
 * For a brand-new conversation there is no cache entry yet, so this also stops
 * the redirect to `/chat/{id}` from flashing a loading skeleton over an answer
 * the user can already see.
 *
 * The assistant row carries the **real** `message_id`, promised to the client in
 * `meta` before the first token and used verbatim by the collector when it writes
 * the row. So the seeded row and the stored row are the same row, and the
 * revalidation below replaces it rather than duplicating it — which is why Step 9
 * minted the id up front instead of correcting a provisional one in `done`.
 */
async function applyOptimisticTurn({
  conversationId,
  messageId,
  userText,
  answer,
  done,
}: {
  conversationId: string;
  messageId: string;
  userText: string;
  answer: string;
  done: DoneEvent;
}) {
  const key = conversationKey(conversationId);

  await globalMutate<ConversationDetail>(
    key,
    (current) => {
      const now = new Date().toISOString();
      const baseSeq = current?.messages.at(-1)?.seq ?? -1;

      const userMessage: Message = {
        id: `local-${messageId}-user`,
        seq: baseSeq + 1,
        role: "user",
        content: [{ type: "text", text: userText }],
        meta: {},
        created_at: now,
      };

      const assistantMessage: Message = {
        id: messageId,
        seq: baseSeq + 2,
        role: "assistant",
        content: [{ type: "text", text: answer }],
        // Field-for-field what the collector is writing to Postgres for this same
        // row, so the optimistic render and the render after a refresh agree.
        meta: {
          provider_used: done.served_by.provider,
          model_used: done.served_by.model,
          slot_used: done.served_by.slot,
          requested_slot: done.requested_slot,
          substituted: done.substituted,
          attempts: done.attempts,
          tokens_in: done.usage.prompt_tokens,
          tokens_out: done.usage.completion_tokens,
          wasted_tokens_out: done.usage.wasted_tokens_out ?? 0,
          degraded: done.degraded,
        } satisfies Partial<MessageMeta>,
        created_at: now,
      };

      const messages = [...(current?.messages ?? []), userMessage, assistantMessage];

      return current
        ? { ...current, messages, updated_at: now }
        : {
            id: conversationId,
            title: null,
            preferred_slot: done.requested_slot,
            pinned_model: null,
            created_at: now,
            updated_at: now,
            messages,
          };
    },
    // Revalidate: the user row's id and both seq numbers are invented here, and
    // the stored ones are authoritative. Safe to do immediately — the gateway's
    // collector commits before the response body closes, so the promise this
    // runs under has already outlived the write.
    { revalidate: true },
  );
}
