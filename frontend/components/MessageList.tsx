"use client";

import { useEffect, useRef } from "react";

import type { PendingTurn as PendingTurnState } from "@/lib/hooks";
import type { Message } from "@/lib/types";
import { MessageTurn } from "./MessageTurn";
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
 */
export function MessageList({
  messages,
  pending,
  onRetry,
  onDismiss,
  onKeepPartial,
}: {
  messages: Message[];
  pending: PendingTurnState | null;
  onRetry: () => void;
  onDismiss: () => void;
  onKeepPartial?: () => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  // Follows the answer as it streams, not only as turns are added — but on
  // `restarts.length` rather than on every delta, so a long answer does not fight
  // a user who has scrolled up to re-read something while it is still writing.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, pending?.status, pending?.restarts.length]);

  return (
    <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-6 px-4 py-6">
      <div role="log" aria-live="polite" aria-label="Conversation" className="flex flex-col gap-6">
        {messages.map((message) => (
          <MessageTurn key={message.id} message={message} />
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

      <div ref={bottomRef} aria-hidden />
    </div>
  );
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
