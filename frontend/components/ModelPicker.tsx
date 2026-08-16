"use client";

import { useId } from "react";

import { cn } from "@/lib/cn";
import { resetsInLabel } from "@/lib/format";
import { slotLabel } from "@/lib/models";
import type { ModelEntry, ModelsResponse, SlotStatus } from "@/lib/types";

/**
 * The client-facing half of `GET /v1/models` (D21, Step 7's counterpart).
 *
 * A native `<select>` rather than a hand-rolled listbox — disabled options are
 * a one-attribute feature on this element and a state machine on anything
 * built from `<div>`s, and the platform already gives keyboard and
 * screen-reader support for free.
 *
 * Three rules, one per status this component ever has to render:
 *
 *   - `rate_limited` / `unavailable` → the option is disabled and its label
 *     carries a relative "resets in ~4 min", built off `resets_at` and
 *     `lib/format.ts`'s `resetsInLabel`.
 *   - `unknown` → selectable, with no status suffix. The gateway not knowing a
 *     slot's budget is not a reason to stop the user from trying it.
 *   - `available` → selectable, no suffix.
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
  const id = useId();
  const entries = models?.data ?? [];
  const knownValue = entries.some((entry) => entry.id === value);

  return (
    <div className={cn("inline-flex items-center", className)}>
      <label htmlFor={id} className="sr-only">
        Model
      </label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className={cn(
          "h-7 max-w-[11rem] truncate rounded-control border border-strong bg-raised px-2",
          "text-xs font-medium text-ink-secondary outline-none",
          "focus-visible:border-accent disabled:cursor-not-allowed disabled:opacity-55",
        )}
      >
        {/* The selected value hasn't loaded into `models` yet (or never will,
            e.g. a stale `preferred_slot`) — show it rather than silently
            switching the control to something the user didn't pick. */}
        {!knownValue && <option value={value}>{slotLabel(value)}</option>}
        {entries.map((entry) => (
          <option key={entry.id} value={entry.id} disabled={isBlocked(entry.status)}>
            {optionLabel(entry)}
          </option>
        ))}
      </select>
    </div>
  );
}

function isBlocked(status: SlotStatus): boolean {
  return status === "rate_limited" || status === "unavailable";
}

function optionLabel(entry: ModelEntry): string {
  const name = slotLabel(entry.id);
  if (!isBlocked(entry.status)) return name;

  const reason = entry.status === "rate_limited" ? "rate limited" : "unavailable";
  const wait = entry.resets_at ? resetsInLabel(entry.resets_at) : "";
  return wait ? `${name} — ${reason}, resets in ${wait}` : `${name} — ${reason}`;
}
