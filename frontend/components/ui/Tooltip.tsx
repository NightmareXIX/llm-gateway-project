"use client";

import { useId, useState, type ReactNode } from "react";

/**
 * A hover/focus tooltip.
 *
 * Opens on focus as well as hover, so the content is reachable by keyboard, and
 * the trigger is described by it rather than labelled — the trigger keeps its
 * own name. Where the tooltip carries information that must not be
 * pointer-only (the attempt trail), the caller *also* renders it inside a
 * `VisuallyHidden`; a tooltip is an affordance, not an accessibility strategy.
 */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactNode }) {
  const id = useId();
  const [open, setOpen] = useState(false);

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      <span aria-describedby={open ? id : undefined} className="inline-flex">
        {children}
      </span>

      {open && (
        <span
          role="tooltip"
          id={id}
          className={[
            "pointer-events-none absolute bottom-[calc(100%+6px)] left-0 z-50 w-max max-w-[18rem]",
            "rounded-control border border-subtle bg-raised px-2.5 py-1.5",
            "text-xs leading-relaxed text-ink shadow-card",
            "animate-fade-rise",
          ].join(" ")}
        >
          {content}
        </span>
      )}
    </span>
  );
}
