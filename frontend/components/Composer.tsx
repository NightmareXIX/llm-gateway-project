"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { cn } from "@/lib/cn";
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
 */
export function Composer({
  onSend,
  onStop,
  pending,
  autoFocus = false,
  placeholder = "Send a message…",
}: {
  onSend: (text: string) => void;
  /** Abort the running stream. Given, the send button becomes Stop while pending. */
  onStop?: () => void;
  pending: boolean;
  autoFocus?: boolean;
  placeholder?: string;
}) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_ROWS_HEIGHT)}px`;
  }, [value]);

  function submit() {
    const text = value.trim();
    if (!text || pending) return;
    onSend(text);
    setValue("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // `isComposing` guards IME input: pressing Enter to commit a candidate in a
    // Japanese or Chinese IME must not send the message.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div
      className="border-t border-subtle bg-ground/85 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <form
        className="mx-auto w-full max-w-[46rem] px-4 py-3"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div
          className={cn(
            "flex items-end gap-2 rounded-card border border-strong bg-raised p-2",
            "transition-colors focus-within:border-accent",
          )}
        >
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
            className="max-h-[200px] min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-[0.9375rem] leading-relaxed text-ink outline-none placeholder:text-ink-tertiary"
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
              disabled={!value.trim() || pending}
              aria-busy={pending || undefined}
              className={cn(
                "inline-flex size-9 shrink-0 items-center justify-center rounded-control",
                "bg-accent text-accent-ink transition-opacity",
                "disabled:cursor-not-allowed disabled:opacity-40",
              )}
            >
              {pending ? (
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
              <span className="sr-only">{pending ? "Sending message" : "Send message"}</span>
            </button>
          )}
        </div>

        <p className="mt-2 px-1 text-[0.6875rem] text-ink-tertiary">
          <kbd className="font-mono">Enter</kbd> to send ·{" "}
          <kbd className="font-mono">Shift+Enter</kbd> for a new line
        </p>
      </form>
    </div>
  );
}
