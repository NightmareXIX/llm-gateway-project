"use client";

import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";
import { cn } from "@/lib/cn";
import { VisuallyHidden } from "./VisuallyHidden";

export type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  /** Required. An icon-only control with no accessible name is unusable. */
  label: string;
  children: ReactNode;
  tone?: "default" | "danger";
  /** React 19 passes `ref` as an ordinary prop — no `forwardRef` wrapper needed. */
  ref?: Ref<HTMLButtonElement>;
};

export function IconButton({ label, children, tone = "default", className, ...rest }: IconButtonProps) {
  return (
    <button
      title={label}
      className={cn(
        "inline-flex size-8 shrink-0 items-center justify-center rounded-control",
        "transition-colors duration-100 disabled:cursor-not-allowed disabled:opacity-55",
        tone === "danger"
          ? "text-ink-tertiary hover:bg-danger-wash hover:text-danger"
          : "text-ink-tertiary hover:bg-sunken hover:text-ink",
        className,
      )}
      {...rest}
    >
      {children}
      <VisuallyHidden>{label}</VisuallyHidden>
    </button>
  );
}
