"use client";

import { absoluteTime } from "@/lib/format";
import { fromMessage } from "@/lib/provenance";
import type { ContentBlock, Message } from "@/lib/types";
import { ModelIndicator } from "./ModelIndicator";

/**
 * One stored turn.
 *
 * The two roles are shaped differently on purpose, and not as mirrored bubbles.
 * A user turn is short and benefits from a contained, tinted block; a model
 * answer is long-form and reads badly inside one — so the assistant turn is
 * full-width prose on the ground colour, capped at a readable measure, with its
 * provenance directly beneath it.
 */
export function MessageTurn({ message }: { message: Message }) {
  if (message.role === "system") return <SystemTurn message={message} />;
  return message.role === "user" ? <UserTurn message={message} /> : <AssistantTurn message={message} />;
}

function UserTurn({ message }: { message: Message }) {
  return (
    <article className="flex justify-end" aria-label="Your message">
      <div className="max-w-[min(42rem,85%)] rounded-card rounded-br-sm border border-subtle bg-raised px-4 py-3 shadow-sm">
        <Blocks blocks={message.content} className="prose-answer text-[0.9375rem] text-ink" />
        <time
          dateTime={message.created_at}
          className="mt-1.5 block text-right text-[0.6875rem] text-ink-tertiary"
        >
          {absoluteTime(message.created_at)}
        </time>
      </div>
    </article>
  );
}

function AssistantTurn({ message }: { message: Message }) {
  const provenance = fromMessage(message);

  return (
    <article className="max-w-[46rem]" aria-label="Model response">
      <Blocks blocks={message.content} className="prose-answer text-[0.9375rem] text-ink" />
      {/* Always rendered when the row carries provenance — this is the D1/D2
          disclosure, not an optional detail. */}
      {provenance && <ModelIndicator provenance={provenance} className="mt-2.5" />}
    </article>
  );
}

function SystemTurn({ message }: { message: Message }) {
  return (
    <article className="max-w-[46rem] rounded-card border border-dashed border-strong bg-sunken/60 px-4 py-3">
      <p className="mb-1 text-[0.6875rem] font-medium uppercase tracking-wide text-ink-tertiary">
        System prompt
      </p>
      <Blocks blocks={message.content} className="prose-answer text-sm text-ink-secondary" />
    </article>
  );
}

/**
 * Canonical content blocks → DOM.
 *
 * The unknown-block fallback is deliberate. `tool_call`, `tool_result` and
 * `summary` are reserved types a later phase will start writing, and a
 * transcript that throws on one would turn a forward-compatible schema into a
 * broken page for everyone with an old conversation.
 */
function Blocks({ blocks, className }: { blocks: ContentBlock[]; className?: string }) {
  return (
    <div className={className}>
      {blocks.map((block, index) => {
        switch (block.type) {
          case "text":
            return <p key={index}>{String((block as { text?: unknown }).text ?? "")}</p>;

          case "omission_marker": {
            const count = Number((block as { omitted_count?: unknown }).omitted_count ?? 0);
            return (
              <p
                key={index}
                className="my-3 flex items-center gap-3 text-xs text-ink-tertiary before:h-px before:flex-1 before:bg-subtle after:h-px after:flex-1 after:bg-subtle"
              >
                {count} earlier {count === 1 ? "message" : "messages"} omitted to fit the context
                window
              </p>
            );
          }

          case "file_ref": {
            const filename = String((block as { filename?: unknown }).filename ?? "attachment");
            return (
              <p
                key={index}
                className="my-1 inline-flex items-center gap-1.5 rounded-control border border-subtle bg-sunken px-2 py-1 text-xs text-ink-secondary"
              >
                <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
                  <path
                    d="M14 3v5h5M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    strokeLinejoin="round"
                  />
                </svg>
                {filename}
              </p>
            );
          }

          default:
            return (
              <p key={index} className="my-1 text-xs italic text-ink-tertiary">
                [unsupported content: {String(block.type)}]
              </p>
            );
        }
      })}
    </div>
  );
}
