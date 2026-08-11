"use client";

import { useId, type InputHTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export type TextFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id"> & {
  label: string;
  hint?: ReactNode;
  /** Field-level validation message. Sets `aria-invalid` and wires `aria-describedby`. */
  error?: string | null;
};

export function TextField({ label, hint, error, className, ...rest }: TextFieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-ink">
        {label}
      </label>

      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={cn(hint ? hintId : null, error ? errorId : null) || undefined}
        className={cn(
          "h-10 w-full rounded-control border bg-raised px-3 text-[0.9375rem] text-ink",
          "placeholder:text-ink-tertiary",
          "transition-colors duration-100",
          "disabled:cursor-not-allowed disabled:opacity-60",
          error ? "border-danger" : "border-strong hover:border-ink-tertiary",
          className,
        )}
        {...rest}
      />

      {hint && !error && (
        <p id={hintId} className="text-xs text-ink-tertiary">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-xs font-medium text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
