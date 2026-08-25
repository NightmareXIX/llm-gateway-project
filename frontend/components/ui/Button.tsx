"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Spinner } from "./Spinner";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";
type Shape = "control" | "card";

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

/**
 * The corner radius, as a prop rather than as a `className` override.
 *
 * `cn` is a plain join, not `tailwind-merge`, so a `rounded-card` passed from
 * outside does not replace the base `rounded-control` — both land on the
 * element and the stylesheet's own order silently decides, which is a coin
 * flip a caller cannot see. Choosing between them here emits exactly one.
 */
const SHAPES: Record<Shape, string> = {
  control: "rounded-control",
  card: "rounded-card",
};

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  shape?: Shape;
  /** Shows a spinner and blocks interaction, without collapsing the button's width. */
  loading?: boolean;
  loadingLabel?: string;
  children?: ReactNode;
};

export function Button({
  variant = "secondary",
  size = "md",
  shape = "control",
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
        "inline-flex items-center justify-center font-medium",
        SHAPES[shape],
        "transition-colors duration-100",
        "disabled:cursor-not-allowed disabled:opacity-55",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading && <Spinner className="size-4" />}
      {/* Children are the flex items, not one wrapped span. The span used to
          swallow an icon and its label into a single item, which put an
          `svg` (a block element under preflight) on its own line above the
          text and left the `gap-*` in SIZES with nothing to space. Only the
          loading label — a bare string standing in for the children — still
          needs a box of its own. */}
      {loading && loadingLabel ? <span>{loadingLabel}</span> : children}
    </button>
  );
}
