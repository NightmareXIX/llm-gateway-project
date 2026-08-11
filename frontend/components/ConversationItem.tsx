"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import Link from "next/link";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/lib/format";
import type { Conversation } from "@/lib/types";
import { IconButton } from "./ui/IconButton";

/**
 * One row in the sidebar.
 *
 * The actions are revealed on hover *and* on focus-within, and are permanently
 * visible on the active row. Hover-only affordances are a keyboard and touch
 * trap: `group-focus-within` is what makes tabbing into a row show the same
 * controls a mouse user sees.
 *
 * A conversation with no title falls back to "New conversation" rather than to
 * a fabricated one. Titles are set by the client after the first turn (Phase 1
 * generates none server-side); a row that never got one is genuinely untitled,
 * and saying so is better than inventing something.
 */
export function ConversationItem({
  conversation,
  active,
  onNavigate,
  onRequestDelete,
  onRenamed,
}: {
  conversation: Conversation;
  active: boolean;
  onNavigate?: () => void;
  onRequestDelete: () => void;
  onRenamed: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(conversation.title ?? "");
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renaming) inputRef.current?.select();
  }, [renaming]);

  async function commit() {
    const title = draft.trim();
    setRenaming(false);

    if (title === (conversation.title ?? "")) return;

    setSaving(true);
    try {
      await api.renameConversation(conversation.id, title === "" ? null : title);
      onRenamed();
    } catch {
      // Put the old title back rather than leaving the row showing an edit that
      // did not persist.
      setDraft(conversation.title ?? "");
    } finally {
      setSaving(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      void commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      setDraft(conversation.title ?? "");
      setRenaming(false);
    }
  }

  if (renaming) {
    return (
      <li>
        <input
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
          onBlur={() => void commit()}
          maxLength={200}
          aria-label="Conversation title"
          className="h-9 w-full rounded-control border border-accent bg-raised px-2.5 text-sm text-ink"
        />
      </li>
    );
  }

  return (
    <li className="group relative">
      <Link
        href={`/chat/${conversation.id}`}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        className={cn(
          "flex h-9 items-center gap-2 rounded-control pl-2.5 pr-16 text-sm transition-colors",
          active
            ? "bg-accent-wash font-medium text-ink"
            : "text-ink-secondary hover:bg-sunken hover:text-ink",
        )}
      >
        <span className="min-w-0 flex-1 truncate">
          {conversation.title ?? <span className="text-ink-tertiary">New conversation</span>}
        </span>
        {saving && <span className="text-xs text-ink-tertiary">saving…</span>}
      </Link>

      <span
        className={cn(
          "pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-xs text-ink-tertiary transition-opacity",
          "group-hover:opacity-0 group-focus-within:opacity-0",
        )}
      >
        {relativeTime(conversation.updated_at)}
      </span>

      <span
        className={cn(
          "absolute right-1 top-1/2 flex -translate-y-1/2 items-center opacity-0 transition-opacity",
          "group-hover:opacity-100 group-focus-within:opacity-100",
        )}
      >
        <IconButton
          label={`Rename ${conversation.title ?? "this conversation"}`}
          className="size-7"
          onClick={() => {
            setDraft(conversation.title ?? "");
            setRenaming(true);
          }}
        >
          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
            <path
              d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3Z"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinejoin="round"
            />
          </svg>
        </IconButton>
        <IconButton
          label={`Delete ${conversation.title ?? "this conversation"}`}
          tone="danger"
          className="size-7"
          onClick={onRequestDelete}
        >
          <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
            <path
              d="M5 7h14M10 7V5h4v2m-7 0 .8 12.1a1 1 0 0 0 1 .9h6.4a1 1 0 0 0 1-.9L17 7"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </IconButton>
      </span>
    </li>
  );
}
