"use client";

import Link from "next/link";

import { GatewayError } from "@/lib/api";
import { useConversation, useSendMessage } from "@/lib/hooks";
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
 */
export function ConversationView({ conversationId }: { conversationId: string }) {
  const { conversation, error, isLoading, mutate } = useConversation(conversationId);
  const { pending, send, retry, dismiss } = useSendMessage(conversationId);

  if (isLoading && !conversation) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto">
          <MessageListSkeleton />
        </div>
        <Composer onSend={send} pending={false} />
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
      <div className="min-h-0 flex-1 overflow-y-auto">
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
          />
        )}
      </div>

      <Composer onSend={send} pending={pending?.status === "sending"} autoFocus />
    </div>
  );
}
