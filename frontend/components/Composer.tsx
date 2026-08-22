"use client";

import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { cn } from "@/lib/cn";
import { ACCEPTED_LABEL, FILE_ACCEPT, MAX_ATTACHMENTS, type SentAttachment } from "@/lib/files";
import { useAttachments } from "@/lib/hooks";
import type { ModelsResponse } from "@/lib/types";
import { AttachmentChip } from "./AttachmentChip";
import { ModelPicker } from "./ModelPicker";
import { Spinner } from "./ui/Spinner";

const MAX_ROWS_HEIGHT = 200;

/**
 * The message box.
 *
 * Enter sends, Shift+Enter breaks the line — the convention every chat client
 * shares, and the one users will try first. The textarea grows with its content
 * up to a cap and then scrolls, so a pasted essay does not push the transcript
 * off screen.
 *
 * It stays mounted and enabled while a message is in flight: composing the next
 * thought while the model works is normal, and disabling the field throws away
 * whatever was typed in the meantime. Only the send button reflects the pending
 * state — and since Step 11 it does more than reflect it: while an answer is
 * streaming the button becomes Stop, which aborts the reader and, through it, the
 * upstream generation the gateway is still paying for.
 *
 * **Attachments live here** (Phase 4, Step 10) rather than in the two calling
 * screens. A file is part of composing a message: it is picked, it is dropped,
 * it is removed before sending, and it is cleared by the same submit that clears
 * the text. Hoisting that state to `ConversationView` and `NewConversation`
 * would put one lifecycle in two places for no gain — so `useAttachments` is
 * called here, and what leaves through `onSend` is the resolved hashes.
 *
 * `useAttachments` uploads on selection, so by the time this component blocks
 * submit on `uploading` it is usually already false; the guard is for the case
 * where somebody drops a 9MB PDF and hits Enter half a second later.
 */
export function Composer({
  onSend,
  onStop,
  pending,
  autoFocus = false,
  placeholder = "Send a message…",
  modelSlot,
  onModelSlotChange,
  models,
}: {
  onSend: (text: string, attachments: SentAttachment[]) => void;
  /** Abort the running stream. Given, the send button becomes Stop while pending. */
  onStop?: () => void;
  pending: boolean;
  autoFocus?: boolean;
  placeholder?: string;
  /** The slot riding on the next `onSend`. Omitted, the picker doesn't render —
   *  `NewConversation` and `ConversationView` both always pass it. */
  modelSlot?: string;
  onModelSlotChange?: (slot: string) => void;
  models?: ModelsResponse;
}) {
  const [value, setValue] = useState("");
  const [dragging, setDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Drag events fire on every child element the pointer crosses, so a plain
  // enter/leave pair flickers the highlight off the moment the cursor moves
  // over the textarea. Counting them is the standard fix.
  const dragDepth = useRef(0);

  const { attachments, add, remove, clear, ready, uploading, atCapacity } = useAttachments();

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_ROWS_HEIGHT)}px`;
  }, [value]);

  const blocked = !value.trim() || pending || uploading;

  function submit() {
    const text = value.trim();
    if (blocked) return;
    onSend(text, ready);
    setValue("");
    clear();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // `isComposing` guards IME input: pressing Enter to commit a candidate in a
    // Japanese or Chinese IME must not send the message.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  function endDrag() {
    dragDepth.current = 0;
    setDragging(false);
  }

  function onDrop(event: DragEvent<HTMLFormElement>) {
    event.preventDefault();
    endDrag();
    const files = Array.from(event.dataTransfer.files ?? []);
    if (files.length) add(files);
  }

  return (
    <div
      className="border-t border-subtle bg-ground/85 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <form
        className="relative mx-auto w-full max-w-[46rem] px-4 py-3"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        onDragEnter={(event) => {
          // Only for an actual file drag. Selecting text in the transcript and
          // dragging it across the composer is not an attachment.
          if (!event.dataTransfer.types.includes("Files")) return;
          dragDepth.current += 1;
          setDragging(true);
        }}
        onDragOver={(event) => {
          if (event.dataTransfer.types.includes("Files")) event.preventDefault();
        }}
        onDragLeave={() => {
          dragDepth.current -= 1;
          if (dragDepth.current <= 0) endDrag();
        }}
        onDrop={onDrop}
      >
        <div
          className={cn(
            "rounded-card border bg-raised transition-colors",
            "focus-within:border-accent",
            dragging ? "border-accent bg-accent-wash/40" : "border-strong",
          )}
        >
          {attachments.length > 0 && (
            <ul className="flex flex-wrap gap-1.5 border-b border-subtle p-2">
              {attachments.map((attachment) => (
                <li key={attachment.id}>
                  <AttachmentChip attachment={attachment} onRemove={() => remove(attachment.id)} />
                </li>
              ))}
            </ul>
          )}

          <div className="flex items-end gap-1 p-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={FILE_ACCEPT}
              // `hidden`, not `sr-only`: a visually-hidden input is still a
              // control in the accessibility tree, so a screen reader would
              // announce an unlabelled "Choose File" button next to the labelled
              // one that actually opens it. `display: none` takes it out of the
              // tree entirely and still responds to a programmatic `.click()`.
              className="hidden"
              tabIndex={-1}
              onChange={(event) => {
                add(Array.from(event.target.files ?? []));
                // Reset, or picking the same file twice in a row fires no
                // `change` event the second time and nothing appears to happen.
                event.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={atCapacity}
              title={
                atCapacity
                  ? `Up to ${MAX_ATTACHMENTS} files per message`
                  : `Attach a file — ${ACCEPTED_LABEL}`
              }
              className={cn(
                "inline-flex size-9 shrink-0 items-center justify-center rounded-control",
                "text-ink-tertiary transition-colors hover:bg-sunken hover:text-ink",
                "disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent",
              )}
            >
              <svg viewBox="0 0 24 24" fill="none" className="size-[18px]" aria-hidden>
                <path
                  d="M17.5 10.5 11 17a3.5 3.5 0 0 1-5-4.9l7.1-7.1a2.5 2.5 0 0 1 3.6 3.5l-7.1 7.1a1.5 1.5 0 0 1-2.1-2.1l6.4-6.4"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span className="sr-only">
                {atCapacity ? `Attachment limit of ${MAX_ATTACHMENTS} reached` : "Attach a file"}
              </span>
            </button>

            <label htmlFor="composer" className="sr-only">
              Message
            </label>
            <textarea
              id="composer"
              ref={textareaRef}
              rows={1}
              value={value}
              autoFocus={autoFocus}
              placeholder={placeholder}
              onChange={(event) => setValue(event.target.value)}
              onKeyDown={onKeyDown}
              className="max-h-[200px] min-h-9 flex-1 resize-none bg-transparent px-1 py-1.5 text-[0.9375rem] leading-relaxed text-ink outline-none placeholder:text-ink-tertiary"
            />

            {pending && onStop ? (
              <button
                type="button"
                onClick={onStop}
                className={cn(
                  "inline-flex size-9 shrink-0 items-center justify-center rounded-control",
                  "bg-accent text-accent-ink transition-opacity hover:opacity-90",
                )}
              >
                <span aria-hidden className="size-3 rounded-[2px] bg-current" />
                <span className="sr-only">Stop generating</span>
              </button>
            ) : (
              <button
                type="submit"
                disabled={blocked}
                aria-busy={pending || undefined}
                className={cn(
                  "inline-flex size-9 shrink-0 items-center justify-center rounded-control",
                  "bg-accent text-accent-ink transition-opacity",
                  "disabled:cursor-not-allowed disabled:opacity-40",
                )}
              >
                {pending || uploading ? (
                  <Spinner className="size-4" />
                ) : (
                  <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
                    <path
                      d="M12 19V5m0 0-6 6m6-6 6 6"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                )}
                <span className="sr-only">
                  {uploading
                    ? "Waiting for the upload to finish"
                    : pending
                      ? "Sending message"
                      : "Send message"}
                </span>
              </button>
            )}
          </div>
        </div>

        <div className="mt-2 flex items-center justify-between gap-2 px-1">
          {/* The privacy disclosure, and it has to be *before* the send rather
              than in a settings page: the gateway may hand this document to a
              model that is not ours in order to read it, and that is the kind of
              thing a person decides with the file in front of them. */}
          <p className="text-[0.6875rem] text-ink-tertiary">
            {ready.length > 0 ? (
              <>
                A model that can read files may be sent{" "}
                {ready.length === 1 ? "this file" : "these files"} in order to describe{" "}
                {ready.length === 1 ? "it" : "them"}.
              </>
            ) : (
              <>
                <kbd className="font-mono">Enter</kbd> to send ·{" "}
                <kbd className="font-mono">Shift+Enter</kbd> for a new line
              </>
            )}
          </p>
          {modelSlot !== undefined && onModelSlotChange && (
            <ModelPicker value={modelSlot} onChange={onModelSlotChange} models={models} />
          )}
        </div>

        {dragging && (
          <div
            aria-hidden
            className={cn(
              "pointer-events-none absolute inset-x-4 inset-y-3 flex items-center justify-center",
              "rounded-card border border-dashed border-accent bg-ground/85 backdrop-blur-sm",
            )}
          >
            <span className="text-sm font-medium text-accent">Drop to attach</span>
          </div>
        )}
      </form>
    </div>
  );
}
