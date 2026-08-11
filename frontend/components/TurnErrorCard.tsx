"use client";

import { GatewayError, NetworkError } from "@/lib/api";
import { describeError } from "./ErrorState";
import { RequestId } from "./RequestId";
import { Button } from "./ui/Button";

/**
 * A failed send, shown in the transcript where the answer would have been.
 *
 * Two things it must not do. It must not swallow the user's text — that is
 * echoed above it, and the message itself is already stored server-side, since
 * the gateway persists the inbound turn before calling the provider. And it
 * must not hide the `request_id`: the error envelope carries one on every
 * failure precisely so a user report maps to a log line.
 */
export function TurnErrorCard({
  error,
  onRetry,
  onDismiss,
}: {
  error: GatewayError | NetworkError | undefined;
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

      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" variant="secondary" onClick={onRetry}>
          Try again
        </Button>
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
