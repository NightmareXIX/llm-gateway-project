"use client";

import { useCallback, useRef, useState } from "react";
import Link from "next/link";

import { GatewayError } from "@/lib/api";
import { DEFAULT_SLOT, useConversation, useModels, useSendMessage } from "@/lib/hooks";
import { Composer } from "./Composer";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { MessageList, MessageListSkeleton } from "./MessageList";

/** How close to the top counts as "at the top". One turn's height, roughly:
 *  far enough that the next page is usually already there when the user gets
 *  there, close enough that it is not fetching history nobody asked for. */
const OLDER_PAGE_TRIGGER_PX = 240;

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
 *
 * The transcript is the newest page plus whatever older pages have been
 * scrolled back to (D48). This component owns the scrolling element, so it is
 * where the scroll-up trigger lives; `useConversation` owns the cursor and the
 * re-entrancy guard, and `MessageList` owns the anchoring that keeps the
 * viewport still while a page lands above it.
 */
export function ConversationView({ conversationId }: { conversationId: string }) {
  const {
    conversation,
    messages,
    error,
    isLoading,
    mutate,
    loadOlder,
    isLoadingOlder,
    hasMore,
  } = useConversation(conversationId);
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

  /**
   * Ask for the next older page when the user reaches the top.
   *
   * `onScroll` fires dozens of times per gesture, so this fires `loadOlder` on
   * every one of them and relies on that function's own guard to collapse them
   * into a single request — the alternative, debouncing here, would still be
   * racing the same state and would only make the window narrower. The
   * threshold is a band rather than `scrollTop === 0` because momentum
   * scrolling regularly stops a few pixels short.
   */
  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container || container.scrollTop > OLDER_PAGE_TRIGGER_PX) return;
    void loadOlder();
  }, [loadOlder]);

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

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        className="min-h-0 flex-1 overflow-y-auto overscroll-y-contain"
      >
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
            hasMore={hasMore}
            isLoadingOlder={isLoadingOlder}
            onLoadOlder={() => void loadOlder()}
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
