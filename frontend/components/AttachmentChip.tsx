"use client";

import type { Attachment } from "@/lib/hooks";
import { cn } from "@/lib/cn";
import { formatBytes } from "@/lib/files";
import { FileIcon } from "./ui/FileIcon";
import { Spinner } from "./ui/Spinner";

/**
 * One file sitting in the composer, waiting to be sent.
 *
 * Four states, one shape. Nothing moves as a chip goes from uploading to ready —
 * a row that reflows when an upload finishes makes the composer jump under a
 * cursor that is mid-sentence — so the icon slot holds a spinner while
 * uploading and the second line carries whatever there is to say: the size once
 * it lands, the reason if it did not.
 *
 * A **failed** chip is the one exception, and deliberately: the reason is the
 * actionable half ("11.0 MB, and the limit is 10.0 MB" is what tells someone
 * what to do next), so it is given room to wrap rather than being truncated
 * into a hover affordance nobody on a touchscreen can reach.
 *
 * A failed chip stays. Both failure kinds are actionable — pick a smaller file,
 * pick a supported one, try again — and a file that disappears with a toast
 * gives the user nothing to act on. Removing it is their decision, and the
 * remove control is the same one every other state has.
 */
export function AttachmentChip({
  attachment,
  onRemove,
}: {
  attachment: Attachment;
  onRemove: () => void;
}) {
  const failed = attachment.status === "rejected" || attachment.status === "failed";
  const uploading = attachment.status === "uploading";

  return (
    <div
      className={cn(
        "group flex items-center gap-2 rounded-control border py-1.5 pl-2 pr-1 transition-colors",
        failed
          ? "max-w-[min(24rem,100%)] items-start border-danger/40 bg-danger-wash/70"
          : "max-w-[15rem] border-subtle bg-sunken",
      )}
    >
      <span
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-[6px]",
          failed ? "mt-px text-danger" : "bg-raised text-ink-secondary",
        )}
      >
        {uploading ? (
          <Spinner className="size-3.5" />
        ) : (
          <FileIcon mime={attachment.file?.mime} className="size-4" />
        )}
      </span>

      <span className="min-w-0 flex-1 leading-tight">
        <span className="block truncate text-xs font-medium text-ink" title={attachment.name}>
          {attachment.name}
        </span>
        <span
          className={cn(
            "block text-[0.6875rem]",
            failed ? "text-danger" : "truncate text-ink-tertiary",
          )}
        >
          {uploading ? "Uploading…" : (attachment.reason ?? formatBytes(attachment.size))}
        </span>
      </span>

      <button
        type="button"
        onClick={onRemove}
        title={`Remove ${attachment.name}`}
        className={cn(
          "inline-flex size-6 shrink-0 items-center justify-center rounded-[6px]",
          "text-ink-tertiary transition-colors hover:bg-raised hover:text-ink",
          failed && "mt-px",
        )}
      >
        <svg viewBox="0 0 24 24" fill="none" className="size-3.5" aria-hidden>
          <path
            d="m6 6 12 12M18 6 6 18"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
        <span className="sr-only">Remove {attachment.name}</span>
      </button>
    </div>
  );
}
