"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Spinner } from "./Spinner";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-ink hover:bg-accent-hover shadow-sm",
  secondary: "bg-raised text-ink border border-strong hover:bg-sunken",
  ghost: "text-ink-secondary hover:bg-sunken hover:text-ink",
  danger: "bg-danger text-danger-ink hover:opacity-90",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-sm gap-1.5",
  md: "h-10 px-4 text-sm gap-2",
};

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  /** Shows a spinner and blocks interaction, without collapsing the button's width. */
  loading?: boolean;
  loadingLabel?: string;
  children?: ReactNode;
};

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  loadingLabel,
  className,
  disabled,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      // `aria-busy` rather than swapping the label: a screen reader user who
      // just pressed "Send" should hear that it is working, not lose the button.
      aria-busy={loading || undefined}
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center rounded-control font-medium",
        "transition-colors duration-100",
        "disabled:cursor-not-allowed disabled:opacity-55",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading && <Spinner className="size-4" />}
      <span>{loading && loadingLabel ? loadingLabel : children}</span>
    </button>
  );
}
