import type { ReactNode } from "react";

/**
 * Available to assistive technology, invisible on screen.
 *
 * Used wherever a visual cue carries meaning that a pointer alone reveals — the
 * attempt trail behind a tooltip, an icon-only control's name, a live region's
 * running commentary.
 */
export function VisuallyHidden({ children }: { children: ReactNode }) {
  return (
    <span className="absolute -m-px size-px overflow-hidden whitespace-nowrap border-0 p-0 [clip:rect(0,0,0,0)]">
      {children}
    </span>
  );
}
