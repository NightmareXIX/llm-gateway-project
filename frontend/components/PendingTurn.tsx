"use client";

/**
 * The two rows shown while a message is in flight.
 *
 * The user's own text is echoed immediately and dimmed — it has not been
 * acknowledged by the server yet, and pretending otherwise is how an optimistic
 * UI lies. The thinking row is a real waiting affordance: Phase 1 has no
 * streaming, so a Groq answer arrives all at once after a few seconds of
 * nothing, and that silence needs to be filled by something other than a frozen
 * screen.
 */
export function PendingTurn({ text }: { text: string }) {
  return (
    <>
      <article className="flex justify-end opacity-60" aria-label="Your message, sending">
        <div className="max-w-[min(42rem,85%)] rounded-card rounded-br-sm border border-subtle bg-raised px-4 py-3">
          <p className="prose-answer text-[0.9375rem] text-ink">{text}</p>
          <p className="mt-1.5 text-right text-[0.6875rem] text-ink-tertiary">Sending…</p>
        </div>
      </article>

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
    </>
  );
}
