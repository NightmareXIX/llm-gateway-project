"use client";

import { GatewayError, NetworkError } from "@/lib/api";
import { describeError } from "./ErrorState";
import { RequestId } from "./RequestId";
import { Button } from "./ui/Button";

/**
 * A failed send, shown in the transcript where the answer would have been.
 *
 * Two things it must not do. It must not swallow the user's text — that is shown
 * above it as a stored row, since the gateway persists the inbound turn before
 * calling the provider. And it must not hide the `request_id`: the error envelope
 * carries one on every failure precisely so a user report maps to a log line.
 *
 * Since Step 11 it also carries the salvage offer. When a stream burns through
 * every candidate mid-generation, `done` hands over the longest buffer any of
 * them managed as `partial_content` — and note what the gateway does *not* do
 * with it: invariant 4 says an unfinished generation is an error rather than a
 * stored message, so nothing was written and the fragment exists only here. It
 * is offered rather than pinned into the transcript, because a fragment nobody
 * finished should not silently take the shape of an answer.
 */
export function TurnErrorCard({
  error,
  partial,
  onKeep,
  onRetry,
  onDismiss,
}: {
  error: GatewayError | NetworkError | undefined;
  /** The longest buffer a failed stream produced, if it was worth keeping. */
  partial?: string;
  onKeep?: () => void;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  const { title, detail } = describeError(error);
  const gatewayError = error instanceof GatewayError ? error : null;

  return (
    <div
      role="alert"
      className="max-w-[46rem] rounded-card border border-subtle bg-danger-wash/70 p-4"
    >
      <p className="text-sm font-medium text-ink">{title}</p>
      <p className="mt-1 text-sm text-ink-secondary">{detail}</p>

      {gatewayError && (
        <p className="mt-2 flex flex-wrap items-center gap-x-3 font-mono text-xs text-ink-tertiary">
          <span>{gatewayError.code}</span>
          {gatewayError.requestId && <RequestId value={gatewayError.requestId} />}
        </p>
      )}

      {partial && (
        <div className="mt-3 rounded-card border border-dashed border-strong bg-sunken/60 p-3">
          <p className="text-[0.6875rem] font-medium uppercase tracking-wide text-ink-tertiary">
            Partial answer
          </p>
          <p className="prose-answer mt-1.5 text-sm text-ink-secondary">{partial}</p>
          <p className="mt-2 text-[0.6875rem] text-ink-tertiary">
            Generated before the last provider gave up. Keeping it puts it on screen for this
            session only — nothing incomplete is written to your history.
          </p>
        </div>
      )}

      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" variant="secondary" onClick={onRetry}>
          Try again
        </Button>
        {partial && onKeep && (
          <Button size="sm" variant="ghost" onClick={onKeep}>
            Keep partial answer
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>

      {/* Honest about a real Phase 1 rough edge rather than quietly producing a
          confusing transcript. See frontend/README.md. */}
      <p className="mt-3 text-xs text-ink-tertiary">
        Your message was saved before the model was called, so retrying adds a second copy of it to
        this conversation.
      </p>
    </div>
  );
}
