"use client";

import { useEffect, type RefObject } from "react";

import type { PendingTurn as PendingTurnState } from "@/lib/hooks";
import type { Message } from "@/lib/types";
import { MessageTurn, type AttachmentReading } from "./MessageTurn";
import { PendingTurn } from "./PendingTurn";
import { TurnErrorCard } from "./TurnErrorCard";
import { Skeleton } from "./ui/Skeleton";

/**
 * The transcript.
 *
 * `role="log"` with `aria-live="polite"` so a new answer is announced without
 * interrupting whatever the user is doing — a chat transcript is the textbook
 * case for a log region.
 *
 * Streaming complicates that, and `PendingTurn` handles it: a bubble that grows
 * by a few characters at a time would have a polite live region re-announcing a
 * half sentence every hundred milliseconds, so the in-flight answer opts out of
 * the region while it is being written and rejoins it once it is stored.
 *
 * Auto-scroll is anchored to message count rather than to a scroll event: it
 * follows new turns, and does not fight a user who has scrolled up to re-read
 * something.
 *
 * `scrollContainerRef` names the actual `overflow-y-auto` element the caller
 * scrolls this list inside of, and the effect below moves *that element's own*
 * `scrollTop` — never `Element.scrollIntoView()`. `scrollIntoView` walks every
 * scrollable ancestor to bring its target into view, including `<html>`/`<body>`
 * if the document is even a pixel taller than the viewport at the moment it
 * fires (exactly what happens mid-stream, when the transcript's height changes
 * every render); that is what let auto-scroll drag the *whole page* past its own
 * content, leaving unpainted space below the composer once the layout settled
 * back down. Setting `scrollTop` on the container this list is actually
 * rendered inside of cannot touch any ancestor's scroll position.
 *
 * The second half of the same bug: once a wheel/trackpad scroll runs the
 * container to *its own* scroll limit, the browser chains the leftover delta
 * to the next scrollable ancestor by default — `overflow-hidden` on the shell
 * blocks that ancestor's own scrollbar but not the chaining itself, so the
 * remainder still reached `<html>`. `overscroll-y-contain` on this container
 * (see `ConversationView`/`NewConversation`) stops the chain at its boundary.
 *
 * The third and last: neither of those two matters if the document is genuinely
 * taller than the viewport, and it was — the `VisuallyHidden` spans inside every
 * `ModelIndicator` are `position: absolute` and, before `ChatShell` was
 * positioned, resolved against the initial containing block, escaping the
 * shell's clip and stretching `<html>` to the depth of the last turn. That one
 * is fixed in `ChatShell`, where the note explaining it lives.
 */
export function MessageList({
  messages,
  pending,
  onRetry,
  onDismiss,
  onKeepPartial,
  scrollContainerRef,
}: {
  messages: Message[];
  pending: PendingTurnState | null;
  onRetry: () => void;
  onDismiss: () => void;
  onKeepPartial?: () => void;
  scrollContainerRef?: RefObject<HTMLDivElement | null>;
}) {
  // Follows the answer as it streams, not only as turns are added — but on
  // `restarts.length` rather than on every delta, so a long answer does not fight
  // a user who has scrolled up to re-read something while it is still writing.
  useEffect(() => {
    const container = scrollContainerRef?.current;
    if (!container) return;
    container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
  }, [scrollContainerRef, messages.length, pending?.status, pending?.restarts.length]);

  return (
    <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-6 px-4 py-6">
      <div role="log" aria-live="polite" aria-label="Conversation" className="flex flex-col gap-6">
        {messages.map((message, index) => (
          <MessageTurn
            key={message.id}
            message={message}
            reading={readingFor(messages, index)}
          />
        ))}

        {/* Not on `failed`: that branch revalidates the transcript, so the user's
            message is already above as a stored row and echoing it again would
            show it twice. */}
        {pending && pending.status !== "failed" && <PendingTurn pending={pending} />}

        {pending?.status === "failed" && (
          <TurnErrorCard
            error={pending.error}
            partial={pending.partial}
            onKeep={onKeepPartial}
            onRetry={onRetry}
            onDismiss={onDismiss}
          />
        )}
      </div>
    </div>
  );
}

/**
 * How this user turn's attachments were read, from the answer that followed.
 *
 * The tier is recorded on the assistant row — it is a property of the request
 * that answered, not of the message that carried the file — but the file itself
 * is on the row above, and that is where a "read by local OCR" badge has to
 * appear to mean anything. So the pairing happens here, in the one component
 * that can see both rows.
 *
 * Deliberately the *immediately* following row, not a search: invariant 3 lets
 * consecutive user messages exist (a client can send two before an answer
 * arrives), and in that case the earlier one genuinely has no verdict yet.
 * Reaching further down the list to find one would attribute an answer's
 * reading to a file it never saw.
 */
function readingFor(messages: Message[], index: number): AttachmentReading | undefined {
  if (messages[index]?.role !== "user") return undefined;
  const next = messages[index + 1];
  if (!next || next.role !== "assistant") return undefined;
  return { tier: next.meta.extraction_tier ?? null, degraded: next.meta.degraded ?? false };
}

/** The transcript's loading state — turn-shaped, alternating, so the page does
 *  not visibly re-lay-out when the real messages arrive. */
export function MessageListSkeleton() {
  return (
    <div
      role="status"
      aria-label="Loading conversation"
      className="mx-auto flex w-full max-w-[46rem] flex-col gap-6 px-4 py-6"
    >
      <div className="flex justify-end">
        <Skeleton className="h-16 w-[60%] rounded-card" />
      </div>
      <div className="space-y-2">
        <Skeleton className="h-4 w-[92%]" />
        <Skeleton className="h-4 w-[85%]" />
        <Skeleton className="h-4 w-[64%]" />
        <Skeleton className="mt-3 h-3 w-40" />
      </div>
      <div className="flex justify-end">
        <Skeleton className="h-10 w-[40%] rounded-card" />
      </div>
      <div className="space-y-2">
        <Skeleton className="h-4 w-[88%]" />
        <Skeleton className="h-4 w-[72%]" />
      </div>
    </div>
  );
}
