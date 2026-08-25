"use client";

import { cn } from "@/lib/cn";
import { describeTier, formatBytes } from "@/lib/files";
import { absoluteTime } from "@/lib/format";
import { fromMessage } from "@/lib/provenance";
import type { ContentBlock, ExtractionTier, Message } from "@/lib/types";
import { Markdown } from "./Markdown";
import { ModelIndicator } from "./ModelIndicator";
import { FileIcon } from "./ui/FileIcon";

/**
 * One stored turn.
 *
 * The two roles are shaped differently on purpose, and not as mirrored bubbles.
 * A user turn is short and benefits from a contained, tinted block; a model
 * answer is long-form and reads badly inside one — so the assistant turn is
 * full-width prose on the ground colour, capped at a readable measure, with its
 * provenance directly beneath it.
 *
 * `reading` is how the turn's attachments reached the model, and it is only ever
 * given for a *user* turn: the tier is recorded on the assistant row that
 * answered, but the file it describes is on the row above it, and a badge that
 * says "read by local OCR" belongs next to the document it is talking about
 * rather than one paragraph below. `MessageList` is what pairs the two.
 */
export function MessageTurn({
  message,
  reading,
}: {
  message: Message;
  reading?: AttachmentReading;
}) {
  if (message.role === "system") return <SystemTurn message={message} />;
  return message.role === "user" ? (
    <UserTurn message={message} reading={reading} />
  ) : (
    <AssistantTurn message={message} />
  );
}

/** The assistant row's verdict on the user row's attachments. */
export type AttachmentReading = { tier: ExtractionTier | null; degraded: boolean };

function UserTurn({ message, reading }: { message: Message; reading?: AttachmentReading }) {
  return (
    <article className="flex justify-end" aria-label="Your message">
      <div className="max-w-[min(42rem,85%)] rounded-card rounded-br-sm border border-subtle bg-raised px-4 py-3 shadow-sm">
        <Blocks
          blocks={message.content}
          reading={reading}
          className="prose-answer text-[0.9375rem] text-ink"
        />
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
      <Blocks blocks={message.content} markdown className="text-[0.9375rem] text-ink" />
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
 * `markdown` is set for assistant turns and nothing else: what the model wrote
 * is markdown and has to be rendered as such, while the user's own text is
 * literal.
 *
 * The unknown-block fallback is deliberate. `tool_call`, `tool_result` and
 * `summary` are reserved types a later phase will start writing, and a
 * transcript that throws on one would turn a forward-compatible schema into a
 * broken page for everyone with an old conversation.
 */
function Blocks({
  blocks,
  className,
  reading,
  markdown = false,
}: {
  blocks: ContentBlock[];
  className?: string;
  reading?: AttachmentReading;
  markdown?: boolean;
}) {
  return (
    <div className={className}>
      {blocks.map((block, index) => {
        switch (block.type) {
          case "text": {
            const text = String((block as { text?: unknown }).text ?? "");
            // Only a model answer is markdown. A user who typed `**hi**` meant
            // the asterisks, and rendering their own text as markdown would
            // silently rewrite what they said back at them.
            return markdown ? <Markdown key={index} text={text} /> : <p key={index}>{text}</p>;
          }

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
            const { filename, mime, bytes } = block as {
              filename?: unknown;
              mime?: unknown;
              bytes?: unknown;
            };
            return (
              <AttachedFile
                key={index}
                filename={String(filename ?? "attachment")}
                mime={typeof mime === "string" ? mime : undefined}
                bytes={typeof bytes === "number" ? bytes : null}
                reading={reading}
              />
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

/**
 * A `file_ref` in stored history: what was attached, and how it was read.
 *
 * The chip is a record rather than a control — the bucket is private, there is
 * no download endpoint and no signed URL is ever minted (D23), so there is
 * deliberately nothing here to click. What it can say is the filename, the size,
 * and — when the answer that followed came out of local OCR — that the model was
 * working from a reading nobody vouches for. That badge is the disclosure a
 * person scrolling back needs, because by then the indicator under the answer
 * has scrolled past and the question "which of these three files went wrong?"
 * has only one place it can be answered.
 */
function AttachedFile({
  filename,
  mime,
  bytes,
  reading,
}: {
  filename: string;
  mime?: string;
  bytes: number | null;
  reading?: AttachmentReading;
}) {
  // Only when the answer was degraded. Every attachment gets a tier, and a chip
  // on every one of them saying "read directly" would be noise on the common
  // path — what a person scrolling back needs is the one that went wrong.
  const tier =
    reading?.degraded && reading.tier
      ? describeTier(reading.tier, "the model", reading.degraded)
      : null;

  return (
    <span className="my-1 flex flex-wrap items-center gap-x-2 gap-y-1">
      <span className="inline-flex max-w-full items-center gap-1.5 rounded-control border border-subtle bg-sunken px-2 py-1 text-xs text-ink-secondary">
        <FileIcon mime={mime} className="size-3.5 text-ink-tertiary" />
        <span className="truncate">{filename}</span>
        {bytes !== null && (
          <span className="shrink-0 text-[0.6875rem] text-ink-tertiary">{formatBytes(bytes)}</span>
        )}
      </span>
      {tier && (
        <span
          className={cn(
            "inline-flex items-center rounded-control px-1.5 py-0.5",
            "bg-warn-wash text-[0.6875rem] font-medium text-warn",
          )}
          title={tier.detail}
        >
          {tier.label}
        </span>
      )}
    </span>
  );
}
