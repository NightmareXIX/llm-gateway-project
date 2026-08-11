"use client";

import { useEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * A modal built on the native `<dialog>` element.
 *
 * `showModal()` gives the focus trap, Escape handling, the inert background and
 * top-layer stacking for free, correctly, in every current browser. Those three
 * are where most hand-rolled modals go wrong, and a dependency for them is not
 * worth it.
 *
 * Focus restoration is the platform's too: `<dialog>` returns focus to whatever
 * was active when it opened.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  /** Body content. */
  children?: ReactNode;
  /** Actions, laid out end-aligned under the body. */
  footer?: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;

    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      // Fires on Escape as well as on close(), so this is the single exit path.
      onClose={onClose}
      onClick={(event) => {
        // Backdrop click: the event target is the dialog itself only when the
        // click landed outside the content box.
        if (event.target === ref.current) onClose();
      }}
      className={cn(
        "m-auto w-[min(28rem,calc(100vw-2rem))] rounded-card border border-subtle",
        "bg-raised p-6 text-ink shadow-card",
        // The backdrop dims *and* blurs, so a modal reads as a layer above the
        // app rather than a box floating on top of a still-legible page.
        "backdrop:bg-overlay backdrop:backdrop-blur-sm",
        "open:animate-fade-rise",
        className,
      )}
    >
      <h2 className="font-serif text-xl leading-snug">{title}</h2>
      {description && <div className="mt-2 text-sm text-ink-secondary">{description}</div>}
      {children && <div className="mt-5">{children}</div>}
      {footer && <div className="mt-6 flex justify-end gap-2">{footer}</div>}
    </dialog>
  );
}
