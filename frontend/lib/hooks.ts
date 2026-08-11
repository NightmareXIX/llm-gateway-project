"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import useSWR, { mutate as globalMutate } from "swr";

import { GatewayError, NetworkError, api, swrFetcher } from "./api";
import { deriveTitle } from "./format";
import type {
  ChatCompletionResponse,
  Conversation,
  ConversationDetail,
  Me,
  Message,
} from "./types";

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

/** What the composer area is currently doing. */
export type PendingTurn = {
  text: string;
  status: "sending" | "failed";
  error?: GatewayError | NetworkError;
};

/**
 * The send path: optimistic user turn, a thinking row, and an honest failure.
 *
 * The optimistic messages are built from the completion response rather than
 * waiting on a refetch, so the answer appears the instant it arrives; SWR then
 * revalidates in the background and the local rows are replaced by the stored
 * ones. On failure the transcript is revalidated too — the gateway persists the
 * user's message *before* calling the provider, so the stored history is the
 * truth and the UI should show it rather than a locally invented state.
 */
export function useSendMessage(conversationId: string | null) {
  const router = useRouter();
  const [pending, setPending] = useState<PendingTurn | null>(null);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      setPending({ text: trimmed, status: "sending" });

      try {
        const response = await api.createCompletion({
          model: DEFAULT_SLOT,
          messages: [{ role: "user", content: trimmed }],
          ...(conversationId ? { conversation_id: conversationId } : {}),
        });

        const isNewConversation = conversationId === null;
        const id = response.conversation_id;

        await applyOptimisticTurn(id, trimmed, response);
        setPending(null);

        if (isNewConversation) {
          // Client-derived title. Phase 1 generates none server-side, so without
          // this every sidebar row reads "New conversation". Deliberately not
          // awaited on the critical path — a failed rename must not look like a
          // failed message.
          void api
            .renameConversation(id, deriveTitle(trimmed))
            .then(() => globalMutate(CONVERSATIONS_KEY))
            .catch(() => globalMutate(CONVERSATIONS_KEY));

          router.replace(`/chat/${id}`);
        } else {
          void globalMutate(CONVERSATIONS_KEY);
        }
      } catch (error) {
        setPending({
          text: trimmed,
          status: "failed",
          error: error instanceof Error ? (error as GatewayError | NetworkError) : undefined,
        });

        // The user's message did land server-side unless the request never
        // arrived. Re-read rather than guess.
        if (conversationId && !(error instanceof NetworkError)) {
          void globalMutate(conversationKey(conversationId));
        }
      }
    },
    [conversationId, router],
  );

  const retry = useCallback(() => {
    if (pending?.status === "failed") void send(pending.text);
  }, [pending, send]);

  const dismiss = useCallback(() => setPending(null), []);

  return { pending, send, retry, dismiss };
}

/**
 * Seed the transcript cache with the turn that just completed.
 *
 * For a brand-new conversation there is no cache entry yet, so this also stops
 * the redirect to `/chat/{id}` from flashing a loading skeleton over an answer
 * the user can already see.
 */
async function applyOptimisticTurn(
  conversationId: string,
  userText: string,
  response: ChatCompletionResponse,
) {
  const key = conversationKey(conversationId);

  await globalMutate<ConversationDetail>(
    key,
    (current) => {
      const now = new Date().toISOString();
      const baseSeq = current?.messages.at(-1)?.seq ?? -1;

      const userMessage: Message = {
        id: `local-${response.message_id}-user`,
        seq: baseSeq + 1,
        role: "user",
        content: [{ type: "text", text: userText }],
        meta: {},
        created_at: now,
      };

      const assistantMessage: Message = {
        id: response.message_id,
        seq: baseSeq + 2,
        role: "assistant",
        content: [{ type: "text", text: response.choices[0]?.message.content ?? "" }],
        meta: {
          provider_used: response.served_by.provider,
          model_used: response.served_by.model,
          slot_used: response.served_by.slot,
          requested_slot: response.requested_slot,
          substituted: response.substituted,
          attempts: response.attempts,
          tokens_in: response.usage.prompt_tokens,
          tokens_out: response.usage.completion_tokens,
          wasted_tokens_out: 0,
          degraded: response.degraded,
        },
        created_at: now,
      };

      const messages = [...(current?.messages ?? []), userMessage, assistantMessage];

      return current
        ? { ...current, messages, updated_at: now }
        : {
            id: conversationId,
            title: null,
            preferred_slot: response.requested_slot,
            pinned_model: null,
            created_at: now,
            updated_at: now,
            messages,
          };
    },
    // Revalidate: the local rows carry invented ids and seq numbers, and the
    // stored ones are authoritative.
    { revalidate: true },
  );
}
