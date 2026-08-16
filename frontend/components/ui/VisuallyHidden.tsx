import type { ReactNode } from "react";

/**
 * Available to assistive technology, invisible on screen.
 *
 * Used wherever a visual cue carries meaning that a pointer alone reveals — the
 * attempt trail behind a tooltip, an icon-only control's name, a live region's
 * running commentary.
 *
 * `absolute` is what keeps it out of layout, and it carries one obligation with
 * it: something above it must be positioned. An absolutely positioned box with
 * no positioned ancestor resolves against the initial containing block, which
 * means no ancestor's `overflow: hidden` clips it and its static position
 * extends the *document's* scroll height instead. Repeat that once per turn in a
 * long transcript and the page grows a scrollbar it was never supposed to have —
 * see the note on `ChatShell`, which is the positioned ancestor for every
 * instance of this component inside the chat routes. The same applies to
 * Tailwind's `sr-only`, which compiles to the same rules.
 */
export function VisuallyHidden({ children }: { children: ReactNode }) {
  return (
    <span className="absolute -m-px size-px overflow-hidden whitespace-nowrap border-0 p-0 [clip:rect(0,0,0,0)]">
      {children}
    </span>
  );
}
