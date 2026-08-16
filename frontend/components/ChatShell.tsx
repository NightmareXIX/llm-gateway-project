"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/cn";
import { ConversationSidebar } from "./ConversationSidebar";
import { IconButton } from "./ui/IconButton";

/**
 * The application frame: a sidebar and a main column.
 *
 * The sidebar is a persistent 17rem column from `lg` up and an off-canvas
 * drawer below it — not a hidden-then-shown copy of itself. One tree, two
 * presentations, so the conversation list has a single source of state and a
 * single set of DOM ids.
 *
 * Height is `100dvh` rather than `100vh`: on mobile the dynamic viewport unit is
 * what keeps the composer above the on-screen keyboard instead of underneath it.
 *
 * The root is `relative`, and that one word is load-bearing rather than leftover.
 * `overflow-hidden` only clips descendants whose containing block is this box or
 * something inside it; an absolutely positioned descendant with *no* positioned
 * ancestor resolves against the initial containing block instead, which puts it
 * outside this clip entirely and lets it contribute to the **document's** scroll
 * height at its static position. The app is full of such boxes — every
 * `sr-only` / `VisuallyHidden` span is `position: absolute` — and
 * `ModelIndicator` renders three per assistant turn, so a long transcript grew
 * `<html>` to the depth of its last turn (7605px against an 861px viewport in
 * the repro) and handed the window a real scrollbar. Scrolling it slid this
 * fixed-height shell up and exposed bare `html` background below the composer.
 * Positioning the shell makes it the containing block for all of them, so the
 * clip above finally applies and the document stays exactly one viewport tall.
 */
export function ChatShell({ children }: { children: ReactNode }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const pathname = usePathname();
  const drawerTriggerRef = useRef<HTMLButtonElement>(null);

  // Navigating closes the drawer — otherwise picking a conversation leaves the
  // overlay sitting on top of the thread it just opened.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!drawerOpen) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setDrawerOpen(false);
        // Focus goes back where it came from, not to the top of the document.
        drawerTriggerRef.current?.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [drawerOpen]);

  return (
    <div className="relative flex h-[100dvh] overflow-hidden bg-ground">
      <a
        href="#composer"
        className="sr-only rounded-control bg-accent px-3 py-2 text-sm text-accent-ink focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50"
      >
        Skip to the message box
      </a>

      {/* Persistent column, lg and up. */}
      <div className="hidden w-[17rem] shrink-0 border-r border-subtle bg-raised lg:block">
        <ConversationSidebar />
      </div>

      {/* Drawer, below lg. */}
      <div
        className={cn(
          "fixed inset-0 z-40 lg:hidden",
          drawerOpen ? "pointer-events-auto" : "pointer-events-none",
        )}
      >
        <div
          onClick={() => setDrawerOpen(false)}
          aria-hidden
          className={cn(
            "absolute inset-0 bg-overlay transition-opacity duration-150",
            drawerOpen ? "opacity-100" : "opacity-0",
          )}
        />
        <div
          role="dialog"
          aria-modal={drawerOpen}
          aria-label="Conversations"
          aria-hidden={!drawerOpen}
          // `inert` keeps the closed drawer out of the tab order without
          // unmounting it, so its scroll position and data survive.
          inert={!drawerOpen}
          className={cn(
            "absolute inset-y-0 left-0 w-[17rem] max-w-[85vw] border-r border-subtle bg-raised",
            "transition-transform duration-150 ease-out",
            drawerOpen ? "translate-x-0" : "-translate-x-full",
          )}
        >
          <ConversationSidebar onNavigate={() => setDrawerOpen(false)} />
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-subtle px-2 lg:hidden">
          <IconButton
            ref={drawerTriggerRef}
            label="Open conversations"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen(true)}
          >
            <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden>
              <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </IconButton>
          <span className="text-sm font-medium text-ink">LLM Gateway</span>
        </header>

        <main className="flex min-h-0 flex-1 flex-col">{children}</main>
      </div>
    </div>
  );
}
