"use client";

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { cn } from "@/lib/cn";
import { resetsInLabel } from "@/lib/format";
import { slotLabel } from "@/lib/models";
import type { ModelEntry, ModelsResponse, SlotStatus } from "@/lib/types";

/**
 * The client-facing half of `GET /v1/models` (D21, Step 7's counterpart).
 *
 * A hand-rolled listbox rather than a native `<select>`. That trade is not
 * free — a `<select>` gives disabled options, keyboard navigation and
 * screen-reader support for one attribute each, and everything below
 * re-implements them. What it cannot give is the *popup*: the option list of a
 * native select is drawn by the operating system, so it cannot carry this
 * app's surfaces, radii or accent, and it renders as a system menu sitting in
 * the middle of a dark theme. A control whose entire job is to disclose
 * per-slot state should not be the one part of the page that looks like it
 * belongs to another program.
 *
 * The three status rules the native version enforced are unchanged, because
 * they are the spec and not the markup:
 *
 *   - `rate_limited` / `unavailable` → the option is unselectable
 *     (`aria-disabled`, skipped by the arrow keys, inert to a click) and its
 *     accessible name carries a relative "resets in ~4 min", built off
 *     `resets_at` and `lib/format.ts`'s `resetsInLabel`.
 *   - `unknown` → selectable, with no status suffix. The gateway not knowing a
 *     slot's budget is not a reason to stop the user from trying it.
 *   - `available` → selectable, no suffix.
 *
 * Each option carries that whole sentence as an explicit `aria-label` while
 * rendering the slot name and its status as two lines. What a screen reader
 * announces is therefore the exact string the native `<option>` used to
 * produce, rather than whatever the two-line layout happens to concatenate to.
 *
 * `models` is `undefined` while the first fetch is in flight or after one
 * fails; the picker still renders the current value as its only option rather
 * than disappearing, since a slot name is still meaningful without live status
 * attached to it.
 */
export function ModelPicker({
  models,
  value,
  onChange,
  disabled = false,
  className,
}: {
  models: ModelsResponse | undefined;
  value: string;
  onChange: (slot: string) => void;
  disabled?: boolean;
  className?: string;
}) {
  const baseId = useId();
  const listId = `${baseId}-list`;
  const labelId = `${baseId}-label`;
  const valueId = `${baseId}-value`;

  const entries = useMemo(() => optionsFor(models, value), [models, value]);
  const selectedIndex = entries.findIndex((entry) => entry.id === value);

  const [open, setOpen] = useState(false);
  const [dropUp, setDropUp] = useState(false);
  const [active, setActive] = useState(0);

  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);

  /**
   * The composer sits at the bottom of the viewport, so this list opens upward
   * far more often than not — measured on open rather than assumed, because the
   * same component renders in other places where there is room below.
   */
  const openList = useCallback(() => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) setDropUp(window.innerHeight - rect.bottom < MENU_CLEARANCE_PX);
    setActive(firstSelectable(entries, selectedIndex < 0 ? 0 : selectedIndex));
    setOpen(true);
  }, [entries, selectedIndex]);

  // A click anywhere else dismisses, the way a native popup does. Bound on
  // `pointerdown` so one click both closes this and lands on what it hit.
  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // Focus follows the popup: into the list on open (it owns the arrow keys via
  // `aria-activedescendant`), back to the trigger on an explicit close.
  useEffect(() => {
    if (open) listRef.current?.focus();
  }, [open]);

  const commit = (index: number) => {
    const entry = entries[index];
    if (!entry || isBlocked(entry.status)) return;
    close(true);
    if (entry.id !== value) onChange(entry.id);
  };

  const onListKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActive((current) => step(entries, current, 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        setActive((current) => step(entries, current, -1));
        break;
      case "Home":
        event.preventDefault();
        setActive(firstSelectable(entries, 0));
        break;
      case "End":
        event.preventDefault();
        setActive(step(entries, entries.length, -1));
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        commit(active);
        break;
      case "Escape":
        event.preventDefault();
        close(true);
        break;
      case "Tab":
        // Deliberately not prevented: Tab should still move on, it just must
        // not leave an orphaned popup behind it.
        close(false);
        break;
    }
  };

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (open) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      // Enter and Space are the button's own activation and already reach
      // `onClick`; preventing them here would open and close in one press.
      event.preventDefault();
      openList();
    }
  };

  return (
    <div ref={rootRef} className={cn("relative inline-flex", className)}>
      <span id={labelId} className="sr-only">
        Model
      </span>

      <button
        ref={triggerRef}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-labelledby={`${labelId} ${valueId}`}
        disabled={disabled}
        onKeyDown={onTriggerKeyDown}
        onClick={() => (open ? close(false) : openList())}
        className={cn(
          "inline-flex h-8 max-w-[11rem] items-center gap-2 rounded-control border py-0 pr-1.5 pl-2.5",
          "bg-raised text-xs font-medium",
          "transition-colors duration-100",
          "disabled:cursor-not-allowed disabled:opacity-55",
          // The accent marks the *open* control and the focus ring, nothing
          // else. A picker outlined in accent at rest would compete with the
          // send button for the one colour this app spends on primary action.
          open
            ? "border-accent text-ink"
            : "border-strong text-ink-secondary hover:border-ink-tertiary hover:text-ink",
        )}
      >
        <span id={valueId} className="truncate">
          {slotLabel(value)}
        </span>
        <Chevron open={open} />
      </button>

      {open && (
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          tabIndex={-1}
          aria-labelledby={labelId}
          aria-activedescendant={entries[active] ? optionId(baseId, active) : undefined}
          onKeyDown={onListKeyDown}
          className={cn(
            "absolute z-50 min-w-full max-w-[16rem] overflow-hidden",
            "rounded-card border border-subtle bg-raised shadow-card outline-none",
            "animate-fade-rise",
            dropUp ? "bottom-[calc(100%+6px)]" : "top-[calc(100%+6px)]",
            // Right-aligned: the picker sits at the end of the composer's
            // footer row, so a left-aligned popup would hang off the card.
            "right-0",
          )}
        >
          {entries.map((entry, index) => {
            const blocked = isBlocked(entry.status);
            const selected = entry.id === value;

            return (
              <li
                key={entry.id}
                id={optionId(baseId, index)}
                role="option"
                aria-label={optionLabel(entry)}
                aria-selected={selected}
                aria-disabled={blocked || undefined}
                onPointerMove={() => !blocked && setActive(index)}
                onClick={() => commit(index)}
                className={cn(
                  "flex flex-col gap-0.5 border-b border-subtle px-3 py-2 last:border-b-0",
                  "cursor-pointer text-xs leading-tight transition-colors duration-75",
                  blocked && "cursor-not-allowed text-ink-tertiary opacity-70",
                  !blocked && selected && "bg-accent text-accent-ink",
                  !blocked && !selected && index === active && "bg-sunken text-ink",
                  !blocked && !selected && index !== active && "text-ink-secondary",
                )}
              >
                <span className="truncate font-medium">{slotLabel(entry.id)}</span>
                {blocked && (
                  <span className="truncate text-[0.6875rem] font-normal">{statusSuffix(entry)}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** How much room below the trigger a downward popup needs before it flips up. */
const MENU_CLEARANCE_PX = 260;

function Chevron({ open }: { open: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-5 shrink-0 items-center justify-center rounded-full",
        "bg-accent-wash text-accent transition-transform duration-100",
        open && "rotate-180",
      )}
    >
      <svg viewBox="0 0 12 12" fill="none" className="size-3">
        <path
          d="M2.5 4.5 6 8l3.5-3.5"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

/**
 * The rows to draw: everything `/v1/models` returned, plus the current value if
 * it is not among them — a slot whose status has not loaded yet, or a stale
 * `preferred_slot` that never will. Showing it beats silently switching the
 * control to something the user did not pick.
 */
function optionsFor(models: ModelsResponse | undefined, value: string): ModelEntry[] {
  const entries = models?.data ?? [];
  if (entries.some((entry) => entry.id === value)) return entries;
  return [placeholderFor(value), ...entries];
}

function placeholderFor(value: string): ModelEntry {
  return {
    id: value,
    object: "model",
    created: 0,
    owned_by: null,
    status: "unknown",
    resets_at: null,
    description: "",
    candidates: [],
  };
}

function optionId(baseId: string, index: number): string {
  return `${baseId}-option-${index}`;
}

function isBlocked(status: SlotStatus): boolean {
  return status === "rate_limited" || status === "unavailable";
}

/** The first selectable index at or after `from`, wrapping; `from` if none is. */
function firstSelectable(entries: ModelEntry[], from: number): number {
  for (let offset = 0; offset < entries.length; offset += 1) {
    const index = (from + offset + entries.length) % entries.length;
    const candidate = entries[index];
    if (candidate && !isBlocked(candidate.status)) return index;
  }
  return from;
}

/** Arrow-key movement: one step in `delta`, skipping blocked rows, no wrap. */
function step(entries: ModelEntry[], from: number, delta: number): number {
  for (let index = from + delta; index >= 0 && index < entries.length; index += delta) {
    const candidate = entries[index];
    if (candidate && !isBlocked(candidate.status)) return index;
  }
  return from >= 0 && from < entries.length ? from : firstSelectable(entries, 0);
}

function statusSuffix(entry: ModelEntry): string {
  const reason = entry.status === "rate_limited" ? "rate limited" : "unavailable";
  const wait = entry.resets_at ? resetsInLabel(entry.resets_at) : "";
  return wait ? `${reason}, resets in ${wait}` : reason;
}

function optionLabel(entry: ModelEntry): string {
  const name = slotLabel(entry.id);
  return isBlocked(entry.status) ? `${name} — ${statusSuffix(entry)}` : name;
}
