"use client";

import { cn } from "@/lib/cn";
import { formatBytes } from "@/lib/files";
import type { PendingTurn as PendingTurnState } from "@/lib/hooks";
import { modelLabel } from "@/lib/models";
import { restartReason } from "@/lib/provenance";
import { Markdown } from "./Markdown";
import { ModelIndicator } from "./ModelIndicator";
import { FileIcon } from "./ui/FileIcon";

/**
 * The turn in flight.
 *
 * The user's own text is echoed immediately and dimmed — it has not been
 * acknowledged by the server yet, and pretending otherwise is how an optimistic
 * UI lies.
 *
 * Below it, the answer as it is being written. **This component renders
 * `pending.answer` and nothing else**, which is what makes §1.1's client-side
 * restart contract structurally true rather than merely intended: the hook
 * clears that string on `restart`, so there is no code path here that could
 * splice one attempt onto another even if someone tried. What the user sees is a
 * bubble that empties, an indicator that changes model, and an answer that starts
 * over — a deliberate product behaviour rather than a glitch.
 *
 * The thinking dots are still a real state and not a legacy of Phase 1: D13 says
 * the gateway sends no byte at all until a provider produces its first token, so
 * the silence before `meta` is genuine and can last seconds.
 */
export function PendingTurn({ pending }: { pending: PendingTurnState }) {
  const restart = pending.restarts.at(-1);

  return (
    <>
      <article className="flex justify-end opacity-60" aria-label="Your message">
        <div className="max-w-[min(42rem,85%)] rounded-card rounded-br-sm border border-subtle bg-raised px-4 py-3">
          <p className="prose-answer text-[0.9375rem] text-ink">{pending.text}</p>
          {/* Same chip the stored row will render a moment later, so the turn
              does not visibly change shape when the optimistic message is
              replaced by the one that came back from the gateway. */}
          {pending.attachments.map((attachment) => (
            <span
              key={attachment.file_hash}
              className="mt-1.5 inline-flex max-w-full items-center gap-1.5 rounded-control border border-subtle bg-sunken px-2 py-1 text-xs text-ink-secondary"
            >
              <FileIcon mime={attachment.mime} className="size-3.5 text-ink-tertiary" />
              <span className="truncate">{attachment.filename}</span>
              <span className="shrink-0 text-[0.6875rem] text-ink-tertiary">
                {formatBytes(attachment.bytes)}
              </span>
            </span>
          ))}
          {/* Dropped once tokens are arriving: by then the message has provably
              landed, and a label still claiming otherwise is just wrong. */}
          {pending.status === "sending" && (
            <p className="mt-1.5 text-right text-[0.6875rem] text-ink-tertiary">Sending…</p>
          )}
        </div>
      </article>

      {pending.status === "sending" ? (
        <Thinking />
      ) : (
        <article
          className="max-w-[46rem]"
          aria-label="Model response"
          // Opted out of the transcript's polite live region while it is being
          // written: a region that re-announces on every delta reads a half
          // sentence aloud ten times a second. The stored row that replaces this
          // one rejoins the region, so the finished answer is still announced.
          aria-live={pending.status === "streaming" ? "off" : "polite"}
          aria-busy={pending.status === "streaming" || undefined}
        >
          {/* Disclosure, not decoration: the answer above was thrown away and this
              one started from nothing, and a user who scrolled away deserves to
              know why the text changed under them. */}
          {restart && (
            <p className="mb-2 text-xs text-warn">
              Restarted on {modelLabel(restart.next.model)} — {restartReason(restart.reason)}
            </p>
          )}

          {/* Rendered as markdown while it streams, exactly as the stored row
              will be a moment later, so the finished answer does not visibly
              reflow when the optimistic turn is replaced. Half-written syntax
              is the price: a fence is a fence only once its closing ``` has
              arrived, so a code block appears as plain text for as long as the
              model is inside it. The caret is attached in CSS (`is-streaming`)
              rather than as an element here, because the last block might be a
              list item or a table cell rather than a paragraph. */}
          <Markdown
            text={pending.answer}
            className={cn(
              "text-[0.9375rem] text-ink",
              pending.status === "streaming" && "is-streaming",
            )}
          />

          {pending.provenance && (
            <ModelIndicator provenance={pending.provenance} className="mt-2.5" />
          )}

          {pending.status === "unsaved" && (
            <p className="mt-2 text-xs text-ink-tertiary">
              Not saved — this answer was cut short, so the gateway stored no message for it and it
              will not survive a refresh.
            </p>
          )}
        </article>
      )}
    </>
  );
}

function Thinking() {
  return (
    <article className="max-w-[46rem]" aria-label="Waiting for the model">
      <span className="inline-flex items-center gap-2 text-sm text-ink-tertiary">
        <span aria-hidden className="flex items-center gap-1">
          {[0, 1, 2].map((dot) => (
            <span
              key={dot}
              className="size-1.5 rounded-full bg-ink-tertiary [animation:thinking-pulse_1.2s_ease-in-out_infinite]"
              style={{ animationDelay: `${dot * 160}ms` }}
            />
          ))}
        </span>
        Thinking
      </span>
    </article>
  );
}

