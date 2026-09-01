"use client";

import { useCallback, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR, { mutate as globalMutate } from "swr";

import { GatewayError, NetworkError, PROVIDER_KEYS_KEY, api, swrFetcher } from "./api";
import { MAX_ATTACHMENTS, rejectionFor, uploadFailureReason, type SentAttachment } from "./files";
import { deriveTitle } from "./format";
import { buildAttemptTrail, fromDoneEvent, fromMetaEvent, type Provenance } from "./provenance";
import { openCompletionStream, type DoneEvent, type MetaEvent, type RestartEvent } from "./sse";
import type {
  ContentBlock,
  Conversation,
  ConversationDetail,
  Me,
  Message,
  MessageMeta,
  ModelsResponse,
  ProviderKeyStatus,
} from "./types";

export const CONVERSATIONS_KEY = "/v1/conversations";
export const conversationKey = (id: string) => `/v1/conversations/${id}`;
export const MODELS_KEY = "/v1/models";

/** The picker's default value. `auto` lets the gateway choose — which is also
 *  why `substituted` can only ever be false for a turn nobody redirected. */
export const DEFAULT_SLOT = "auto";

export function useMe() {
  const { data, error, isLoading } = useSWR<Me>("/v1/me", swrFetcher, {
    revalidateOnFocus: false,
  });
  return { me: data, error, isLoading };
}

/**
 * Live per-slot status, for `ModelPicker`.
 *
 * Fetched on mount like every other SWR hook here, and additionally revalidated
 * by `useSendMessage` after every completed turn — a status that only refreshes
 * on page load is wrong exactly when it matters, which is the moment a slot a
 * user is actively hammering flips to `rate_limited` mid-session.
 */
export function useModels() {
  const { data, error, isLoading, mutate } = useSWR<ModelsResponse>(MODELS_KEY, swrFetcher, {
    revalidateOnFocus: false,
  });
  return { models: data, error, isLoading, mutate };
}

export function useConversations() {
  const { data, error, isLoading, mutate } = useSWR<Conversation[]>(
    CONVERSATIONS_KEY,
    swrFetcher,
  );
  return { conversations: data, error, isLoading, mutate };
}

/**
 * One thread: the newest page from SWR, older pages from component state.
 *
 * **The split is the whole point of D48's two routes.** The head — what
 * `GET /v1/conversations/{id}` returns — is live: an optimistic turn appends to
 * it and a revalidation replaces it wholesale. Older pages are immutable
 * history, and they are held *here*, outside the SWR cache, so that neither of
 * those two writes can touch them. Written into the head key instead, every
 * `globalMutate(conversationKey(id))` in this file would silently drop every
 * page the user had scrolled back through — and the symptom, a thread getting
 * shorter after you send a message, reads as data loss (trap 12).
 *
 * The paging state is stored *against the conversation id it was loaded for*,
 * the same shape `ConversationView` uses for the model pick: switching threads
 * then resets the cursor and the loaded pages for free, with no effect to run
 * and nothing to clean up on a fetch that is still in flight for the thread
 * the user just left.
 */
export function useConversation(id: string | null) {
  const { data, error, isLoading, mutate } = useSWR<ConversationDetail>(
    id ? conversationKey(id) : null,
    swrFetcher,
    // A transcript is immutable history plus whatever this tab just sent. Focus
    // revalidation would re-fetch every time the user tabs back for no new data.
    { revalidateOnFocus: false },
  );

  const [paging, setPaging] = useState<ConversationPaging | null>(null);
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  // Not derived from `isLoadingOlder`: a state update is not visible to the
  // synchronous caller that queued it, so two scroll events in the same frame
  // would both pass the check and fetch the same page twice. The ref is read
  // and written in the same tick as the guard it protects.
  const inFlightRef = useRef(false);

  // `null` for any thread but the one this state was loaded for — including the
  // moment right after a navigation, before the fetch for the new id resolves.
  const loaded = paging?.conversationId === id ? paging : null;

  // The cursor is the newest thing that knows it: the last page fetched, or —
  // before any older page exists — the head response itself. Both fields are
  // optional on the wire (a server older than this build sends neither), and an
  // absent `has_more` means "one page, nothing older", never "unknown".
  const hasMore = loaded ? loaded.hasMore : (data?.has_more ?? false);
  const nextBeforeSeq = loaded ? loaded.nextBeforeSeq : (data?.next_before_seq ?? null);

  const loadOlder = useCallback(async () => {
    if (id === null || inFlightRef.current || !hasMore || nextBeforeSeq === null) return;
    inFlightRef.current = true;
    setIsLoadingOlder(true);
    try {
      const page = await api.fetchMessagePage(id, nextBeforeSeq);
      setPaging((current) => {
        const pages = current?.conversationId === id ? current.pages : [];
        return {
          conversationId: id,
          // Oldest page first in the array, so `pages.flat()` is already in
          // transcript order: each fetch reaches further back than the last.
          pages: [page.messages, ...pages],
          hasMore: page.has_more,
          nextBeforeSeq: page.next_before_seq,
        };
      });
    } catch {
      // Swallowed deliberately, and this is the one place in this file that
      // does it. Every other failure here has something to say — a turn that
      // did not send, a key that was rejected — and a surface to say it on. A
      // page of old history that did not arrive has neither: the transcript on
      // screen is unchanged and still correct, the cursor is untouched, and
      // the trigger comes back enabled, which *is* the retry. Rethrowing would
      // only produce an unhandled rejection at the two `void loadOlder()` call
      // sites, since neither a scroll event nor a click has anywhere to catch.
    } finally {
      inFlightRef.current = false;
      setIsLoadingOlder(false);
    }
  }, [id, hasMore, nextBeforeSeq]);

  const older = loaded ? loaded.pages.flat() : [];
  const head = data?.messages ?? [];
  // De-duplicated in the head's favour: it is the copy a revalidation just
  // refreshed, and the only rows that can appear twice are ones a page boundary
  // and a concurrent write disagreed about.
  const headIds = new Set(head.map((message) => message.id));
  const messages = [...older.filter((message) => !headIds.has(message.id)), ...head];

  return {
    conversation: data,
    /** The whole transcript on screen: every older page loaded, then the head. */
    messages,
    error,
    isLoading,
    mutate,
    loadOlder,
    isLoadingOlder,
    hasMore,
  };
}

/** Older pages of one thread, kept against the id they belong to. */
type ConversationPaging = {
  conversationId: string;
  /** Oldest page first. Each entry is one response, oldest-first within itself. */
  pages: Message[][];
  hasMore: boolean;
  nextBeforeSeq: number | null;
};

// --------------------------------------------------------------------------- //
// BYOK provider keys (Phase 6, Step 10)
// --------------------------------------------------------------------------- //
/**
 * The settings page's rows, and the three writes that change them.
 *
 * **Every write revalidates `/v1/models` as well as this list**, and that is not
 * belt-and-braces: §9.7 lets a private key unlock a slot nobody else can see
 * (`pro`, on a Gemini Pro model the shared free-tier key cannot reach), and the
 * per-candidate status of every *other* slot is computed under the caller's own
 * scope (§9.4) — so adding or removing a key changes what the picker should
 * offer and what each entry's status means. A stale picker after a successful
 * add is the one place a user would conclude the feature does not work.
 *
 * The three writes deliberately do **not** swallow their errors. Which failure
 * happened is the entire message: a 422 is the provider's own wording about the
 * key, a 503 is "we could not check", and a 429 is D43's five-an-hour floor.
 * `ProviderKeysSection` renders each one differently, inline and next to the
 * row it belongs to, so it needs the `GatewayError` itself rather than a
 * boolean.
 */
export function useProviderKeys() {
  const { data, error, isLoading, mutate } = useSWR<ProviderKeyStatus[]>(
    PROVIDER_KEYS_KEY,
    swrFetcher,
    { revalidateOnFocus: false },
  );

  const refresh = useCallback(async () => {
    await mutate();
    await globalMutate(MODELS_KEY);
  }, [mutate]);

  const add = useCallback(
    async (provider: string, key: string) => {
      await api.addProviderKey(provider, key);
      await refresh();
    },
    [refresh],
  );

  const remove = useCallback(
    async (provider: string) => {
      await api.removeProviderKey(provider);
      await refresh();
    },
    [refresh],
  );

  /** D40's payoff: re-ask the provider about a key a live request found broken. */
  const revalidate = useCallback(
    async (provider: string) => {
      await api.revalidateProviderKey(provider);
      await refresh();
    },
    [refresh],
  );

  return { rows: data, error, isLoading, add, remove, revalidate };
}

// --------------------------------------------------------------------------- //
// Attachments (Phase 4, Step 10)
// --------------------------------------------------------------------------- //
/**
 * One file in the composer, at whatever point of its short life it is at.
 *
 * `rejected` and `failed` are two different things and are kept apart: the
 * first never left the browser (`lib/files.ts`'s gate said no), the second was
 * refused by the gateway. Both show the file with its reason rather than
 * vanishing into a toast — the user chose that file, and the chip is where the
 * explanation belongs.
 */
export type Attachment = {
  /** Client-side identity. Selecting the same file twice makes two chips; the
   *  gateway dedups the *bytes*, which is a different question (D24). */
  id: string;
  name: string;
  size: number;
  status: "uploading" | "ready" | "rejected" | "failed";
  /** Set once `status === "ready"` — the hash and the metadata a turn needs. */
  file?: SentAttachment;
  /** Set on `rejected` and `failed`. One sentence, rendered on the chip. */
  reason?: string;
};

/**
 * The composer's attachment list, and the uploads behind it.
 *
 * **The upload happens on selection, not on send.** That is the whole reason
 * this is a hook with its own lifecycle rather than a `FormData` assembled at
 * submit time: by the time the user finishes typing, the hash is already in
 * hand, so pressing Enter costs exactly what it costs without a file. It also
 * means a 413 or a 415 is reported next to the file that caused it, seconds
 * before the message exists — instead of failing a send the user has already
 * committed to.
 *
 * The list is mirrored into a ref because `add` has to know how many slots are
 * left *and* start uploads for what it accepted, and doing both inside a
 * functional `setState` updater would run the side effect twice under React's
 * StrictMode. The ref is the value; `setItems` is how it gets rendered.
 */
export function useAttachments() {
  const [items, setItems] = useState<Attachment[]>([]);
  const itemsRef = useRef<Attachment[]>([]);
  const uploadsRef = useRef(new Map<string, AbortController>());

  const commit = useCallback((next: Attachment[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  const patch = useCallback(
    (id: string, changes: Partial<Attachment>) => {
      commit(
        itemsRef.current.map((item) => (item.id === id ? { ...item, ...changes } : item)),
      );
    },
    [commit],
  );

  const upload = useCallback(
    async (id: string, file: File) => {
      const controller = new AbortController();
      uploadsRef.current.set(id, controller);
      try {
        const uploaded = await api.uploadFile(file, controller.signal);
        patch(id, {
          status: "ready",
          file: {
            file_hash: uploaded.file_hash,
            filename: uploaded.filename,
            // The **sniffed** mime, not the one the browser declared: a PNG
            // named `report.pdf` is stored as an image, and the chip should
            // say what the gateway actually kept.
            mime: uploaded.mime,
            bytes: uploaded.bytes,
          },
        });
      } catch (error) {
        // Removed while in flight — the chip is already gone, and patching a
        // row that no longer exists would resurrect nothing but would log a
        // failure nobody can dismiss.
        if (controller.signal.aborted) return;
        patch(id, { status: "failed", reason: uploadFailureReason(error) });
      } finally {
        uploadsRef.current.delete(id);
      }
    },
    [patch],
  );

  /** Queue files: rejected ones become chips with a reason, the rest upload. */
  const add = useCallback(
    (files: File[]) => {
      const next = [...itemsRef.current];
      const queued: { id: string; file: File }[] = [];
      let room = MAX_ATTACHMENTS - next.length;

      for (const file of files) {
        // Silently dropping the overflow rather than reporting it: the attach
        // button is already disabled at the cap, so the only way to get here is
        // a multi-select or a drop, where "four of these six" is obvious from
        // what appeared.
        if (room <= 0) break;
        room -= 1;

        const id = `${Date.now().toString(36)}-${next.length}-${file.name}`;
        const reason = rejectionFor(file);
        next.push({
          id,
          name: file.name,
          size: file.size,
          status: reason ? "rejected" : "uploading",
          ...(reason ? { reason } : {}),
        });
        if (!reason) queued.push({ id, file });
      }

      commit(next);
      for (const item of queued) void upload(item.id, item.file);
    },
    [commit, upload],
  );

  const remove = useCallback(
    (id: string) => {
      uploadsRef.current.get(id)?.abort();
      uploadsRef.current.delete(id);
      commit(itemsRef.current.filter((item) => item.id !== id));
    },
    [commit],
  );

  /** After a successful send. Deliberately does not abort: everything still
   *  uploading was, by `blocked` below, not part of the message that went. */
  const clear = useCallback(() => commit([]), [commit]);

  return {
    attachments: items,
    add,
    remove,
    clear,
    /** What rides on the next message. */
    ready: items.flatMap((item) => (item.file ? [item.file] : [])),
    /** True while any upload is in flight — the send button waits for it, so a
     *  message can never be sent referencing a hash that does not exist yet. */
    uploading: items.some((item) => item.status === "uploading"),
    atCapacity: items.length >= MAX_ATTACHMENTS,
  };
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
  /** What rode along with `text`, for the optimistic user bubble and for
   *  `retry` — a retried turn has to carry the same files, and re-selecting
   *  them by hand would be the worst possible recovery. */
  attachments: SentAttachment[];
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
 *
 * `slot` rides on every `send` as the request's `model`. It is a plain
 * parameter rather than something read off a ref because `ModelPicker` changes
 * it between turns via `useState` in the calling component, and the value that
 * belongs on the *next* message is whatever is selected right now.
 */
export function useSendMessage(conversationId: string | null, slot: string = DEFAULT_SLOT) {
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
    async (text: string, attachments: SentAttachment[] = []) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      const controller = new AbortController();
      abortRef.current = controller;
      resetTurn();

      setPending({
        text: trimmed,
        attachments,
        status: "sending",
        answer: "",
        provenance: null,
        restarts: [],
      });

      try {
        await openCompletionStream(
          {
            model: slot,
            messages: [
              {
                role: "user",
                content: trimmed,
                // Per *message*, not per request: a `file_ref` is content, and
                // content belongs to the message it was attached to. Omitted
                // entirely when empty rather than sent as `[]`, so an ordinary
                // turn's body is byte-for-byte what it has always been.
                ...(attachments.length
                  ? { file_refs: attachments.map((attachment) => attachment.file_hash) }
                  : {}),
              },
            ],
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
        // A pre-stream failure is exactly the moment a slot's status might have
        // just changed — a quota-exhausted candidate is what produces it.
        if (!(error instanceof NetworkError)) void globalMutate(MODELS_KEY);
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
        void globalMutate(MODELS_KEY);
        return;
      }

      const isNewConversation = conversationId === null;
      const id = meta.conversation_id;

      await applyOptimisticTurn({
        conversationId: id,
        messageId: meta.message_id,
        userText: trimmed,
        attachments,
        answer: answerRef.current,
        done,
      });
      setPending(null);
      // A completed turn is the moment a status the picker shows might be stale
      // — the slot that just answered may now be closer to its own limit.
      void globalMutate(MODELS_KEY);

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
    [conversationId, slot, router, resetTurn],
  );

  /** Abort the in-flight stream. Not an error — a person changing their mind. */
  const stop = useCallback(() => abortRef.current?.abort(), []);

  const retry = useCallback(() => {
    if (pending?.status === "failed") void send(pending.text, pending.attachments);
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
  attachments,
  answer,
  done,
}: {
  conversationId: string;
  messageId: string;
  userText: string;
  attachments: SentAttachment[];
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
        // Text first, then one `file_ref` block per attachment — the exact
        // order `_resolve_file_refs` writes server-side, so the optimistic row
        // and the row the revalidation replaces it with render identically.
        content: [
          { type: "text", text: userText },
          ...attachments.map(
            (attachment): ContentBlock => ({ type: "file_ref", ...attachment }),
          ),
        ],
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
          // The perception disclosure survives the optimistic render too — an
          // answer that says "read by local OCR" must not lose that label for
          // the second between `done` and the revalidation.
          extraction_tier: done.extraction_tier ?? null,
          // And the truncation one (D34), for exactly the same reason: an
          // answer built on two thirds of the thread must not look like a
          // whole-history answer for the second before the refetch lands.
          messages_dropped: done.messages_dropped ?? 0,
          // And the pool disclosure (D42). Same reasoning a third time: a turn
          // the user's own key paid for must not read as a shared-pool turn for
          // the second between `done` and the revalidation.
          key_pool: done.key_pool ?? null,
        } satisfies Partial<MessageMeta>,
        created_at: now,
      };

      const messages = [...(current?.messages ?? []), userMessage, assistantMessage];

      return current
        ? { ...current, messages, updated_at: now }
        : {
            id: conversationId,
            title: null,
            // No longer a guess. Phase 5 (D33) made `preferred_slot` a column
            // the gateway updates on every turn from the request's own `model`,
            // so this optimistic value now *mirrors* what the revalidation will
            // return rather than inventing something the server never stored.
            // Kept rather than dropped: the refetch is a round trip away, and
            // the composer reads this to decide which slot it opens on.
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
