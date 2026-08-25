"use client";

import { GatewayError, NetworkError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ATTACHMENT_ERROR_COPY } from "@/lib/files";
import { Button } from "./ui/Button";
import { RequestId } from "./RequestId";

/**
 * The one place an async failure is turned into words.
 *
 * Two distinctions the copy has to preserve, because they lead to different
 * user actions: "we could not reach the gateway" (check your connection, retry)
 * is not "the gateway said no" (something is wrong upstream, here is the id to
 * quote). And a `GatewayError` always renders its `code` and `request_id` —
 * that pairing is the entire reason the error envelope carries them.
 *
 * Branching on `code` before `status` is the rule, not a shortcut: the code is
 * the stable half of the envelope, and two different 422s (a malformed body and
 * a document nothing could read) are two different things to tell somebody.
 */
export function describeError(error: unknown): { title: string; detail: string } {
  if (error instanceof NetworkError) {
    return {
      title: "Can't reach the gateway",
      detail: "The service didn't respond. Check that it's running, then try again.",
    };
  }

  if (error instanceof GatewayError) {
    // The attachment-shaped failures, in their own words. A 413 that reads
    // "that didn't work" tells a user nothing they can act on; "the limit is
    // 10.0 MB" tells them exactly what to do next. Shared with the failed-chip
    // copy in `lib/files.ts` so the two never disagree about the same refusal.
    const attachment = ATTACHMENT_ERROR_COPY[error.code];
    if (attachment) return attachment;

    if (error.isRateLimited) {
      // Not a failure — a wait. The gateway (or, once our own limit exists, its
      // own guard) said exactly how much of its budget is left, not that
      // anything is broken, and the copy should say that rather than "that
      // didn't work".
      return {
        title: "Every model is at its limit right now",
        detail:
          error.retryAfterS !== null
            ? `Try again in about ${formatWaitSeconds(error.retryAfterS)}.`
            : "Try again shortly, or pick a different model above.",
      };
    }
    if (error.status === 502) {
      return {
        title: "The model provider failed",
        detail: error.message,
      };
    }
    if (error.status >= 500) {
      return { title: "Something went wrong", detail: error.message };
    }
    return { title: "That didn't work", detail: error.message };
  }

  return {
    title: "Something went wrong",
    detail: "An unexpected error occurred. Try again in a moment.",
  };
}

/** Shared with `ProviderKeysSection`, so the two places that turn a wait into
 *  words round it the same way. */
export function formatWaitSeconds(seconds: number): string {
  if (seconds < 60) return `${Math.ceil(seconds)}s`;
  return `${Math.ceil(seconds / 60)} min`;
}

export function ErrorState({
  error,
  onRetry,
  className,
  compact = false,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}) {
  const { title, detail } = describeError(error);
  const gatewayError = error instanceof GatewayError ? error : null;

  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-start gap-3 rounded-card border border-subtle",
        "bg-danger-wash/60 text-left",
        compact ? "p-4" : "p-6",
        className,
      )}
    >
      <div className="flex items-start gap-2.5">
        <WarningIcon />
        <div className="space-y-1">
          <p className="font-medium text-ink">{title}</p>
          <p className="text-sm text-ink-secondary">{detail}</p>
        </div>
      </div>

      {gatewayError && (
        <dl className="flex flex-wrap items-center gap-x-4 gap-y-1 pl-[26px] font-mono text-xs text-ink-tertiary">
          <div className="flex gap-1.5">
            <dt className="sr-only">Error code</dt>
            <dd>{gatewayError.code}</dd>
          </div>
          {gatewayError.requestId && (
            <div className="flex items-center gap-1.5">
              <dt>request</dt>
              <dd>
                <RequestId value={gatewayError.requestId} />
              </dd>
            </div>
          )}
        </dl>
      )}

      {onRetry && (
        <div className="pl-[26px]">
          <Button size="sm" variant="secondary" onClick={onRetry}>
            Try again
          </Button>
        </div>
      )}
    </div>
  );
}

function WarningIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden>
      <path
        d="M12 9v4m0 3h.01M10.3 4.2 2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
