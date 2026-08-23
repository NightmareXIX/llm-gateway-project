"use client";

import { useRef, useState } from "react";
import Link from "next/link";

import { GatewayError } from "@/lib/api";
import { DEFAULT_SLOT, useConversation, useModels, useSendMessage } from "@/lib/hooks";
import { Composer } from "./Composer";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { MessageList, MessageListSkeleton } from "./MessageList";

/**
 * One thread.
 *
 * Four states, and the 404 is the one worth being careful about: the gateway
 * answers "not yours" and "doesn't exist" identically by design — ownership is
 * scoped inside the SQL, and a 403 would confirm that the id names something
 * real. So this renders one honest message for both, with a way back, rather
 * than an error that speculates about which it was.
 *
 * The picker's starting value is the thread's own `preferred_slot` (D33): pick
 * `fast` on turn nine, reload, and you come back on `fast` rather than
 * silently on `auto`. See `modelSlot` below for why that is a derivation and
 * not a `useEffect`.
 */
export function ConversationView({ conversationId }: { conversationId: string }) {
  const { conversation, error, isLoading, mutate } = useConversation(conversationId);
  const { models } = useModels();

  /**
   * The slot this composer sends on, and the load race it has to survive.
   *
   * The conversation arrives *after* first paint, so the two obvious shapes
   * both break: `useState(conversation?.preferred_slot ?? DEFAULT_SLOT)`
   * captures `undefined` on the first render and never updates, and a
   * `useEffect` that syncs on every change stomps a slot the user picked while
   * the fetch was still in flight.
   *
   * So the value is *derived* rather than synchronised. An explicit pick wins
   * for the thread it was made on; otherwise the stored preference wins once it
   * loads; otherwise `DEFAULT_SLOT`. There is no effect, nothing to adopt
   * "once", and navigating to another thread resets it for free, because the
   * pick is stored against the id it was made under.
   *
   * A **preference**, not a pin: this seeds the control and the user overrides
   * it in one click. `conversations.pinned_model` is the other kind of fact —
   * a constraint the router enforces and the client cannot override at all —
   * and it never reaches this state.
   */
  const [pick, setPick] = useState<{ conversationId: string; slot: string } | null>(null);
  const modelSlot =
    (pick?.conversationId === conversationId ? pick.slot : null) ??
    conversation?.preferred_slot ??
    DEFAULT_SLOT;
  const setModelSlot = (slot: string) => setPick({ conversationId, slot });

  const { pending, send, stop, retry, keepPartial, dismiss } = useSendMessage(
    conversationId,
    modelSlot,
  );
  const busy = pending?.status === "sending" || pending?.status === "streaming";
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  if (isLoading && !conversation) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-y-contain">
          <MessageListSkeleton />
        </div>
        <Composer
          onSend={send}
          pending={false}
          modelSlot={modelSlot}
          onModelSlotChange={setModelSlot}
          models={models}
        />
      </div>
    );
  }

  if (error instanceof GatewayError && error.isNotFound) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <EmptyState
          size="lg"
          title="This conversation isn't here"
          description="It may have been deleted, or the link belongs to another account."
          action={
            <Link
              href="/chat"
              className="inline-flex h-10 items-center justify-center rounded-control border border-strong bg-raised px-4 text-sm font-medium text-ink transition-colors hover:bg-sunken"
            >
              Start a new conversation
            </Link>
          }
        />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <ErrorState className="max-w-md" error={error} onRetry={() => void mutate()} />
      </div>
    );
  }

  const messages = conversation?.messages ?? [];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scrollContainerRef} className="min-h-0 flex-1 overflow-y-auto overscroll-y-contain">
        {messages.length === 0 && pending === null ? (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              title="Nothing here yet"
              description="Send the first message in this conversation."
            />
          </div>
        ) : (
          <MessageList
            messages={messages}
            pending={pending}
            onRetry={retry}
            onDismiss={dismiss}
            onKeepPartial={keepPartial}
            scrollContainerRef={scrollContainerRef}
          />
        )}
      </div>

      <Composer
        onSend={send}
        onStop={stop}
        pending={busy}
        autoFocus
        modelSlot={modelSlot}
        onModelSlotChange={setModelSlot}
        models={models}
      />
    </div>
  );
}
