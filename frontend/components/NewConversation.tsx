"use client";

import { useState } from "react";

import { DEFAULT_SLOT, useModels, useSendMessage } from "@/lib/hooks";
import { Composer } from "./Composer";
import { EmptyState } from "./EmptyState";
import { MessageList } from "./MessageList";

/**
 * The opening screen: no conversation yet, just an invitation and a composer.
 *
 * The conversation is created by the first send — the gateway returns a
 * `conversation_id` on the completion response, so there is no separate
 * "create thread" call and nothing to clean up if the user changes their mind.
 * `useSendMessage` seeds the transcript cache and then swaps the route to
 * `/chat/{id}`, so the answer is on screen before the navigation lands.
 */
export function NewConversation() {
  const { models } = useModels();
  const [modelSlot, setModelSlot] = useState(DEFAULT_SLOT);
  const { pending, send, stop, retry, keepPartial, dismiss } = useSendMessage(null, modelSlot);
  const busy = pending?.status === "sending" || pending?.status === "streaming";
  const showTranscript = pending !== null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        {showTranscript ? (
          <MessageList
            messages={[]}
            pending={pending}
            onRetry={retry}
            onDismiss={dismiss}
            onKeepPartial={keepPartial}
          />
        ) : (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              size="lg"
              title="What are we working on?"
              description="Ask anything. The gateway picks a model, answers, and tells you underneath which one actually did."
            />
          </div>
        )}
      </div>

      <Composer
        onSend={send}
        onStop={stop}
        pending={busy}
        autoFocus
        placeholder="Start a conversation…"
        modelSlot={modelSlot}
        onModelSlotChange={setModelSlot}
        models={models}
      />
    </div>
  );
}
