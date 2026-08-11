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
 * case for a log region, and Phase 1's non-streamed answers arrive as one
 * complete addition, which is exactly what a polite live region handles well.
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
}: {
  messages: Message[];
  pending: PendingTurnState | null;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, pending?.status]);

  return (
    <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-6 px-4 py-6">
      <div role="log" aria-live="polite" aria-label="Conversation" className="flex flex-col gap-6">
        {messages.map((message) => (
          <MessageTurn key={message.id} message={message} />
        ))}

        {pending?.status === "sending" && <PendingTurn text={pending.text} />}

        {pending?.status === "failed" && (
          <TurnErrorCard error={pending.error} onRetry={onRetry} onDismiss={onDismiss} />
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
