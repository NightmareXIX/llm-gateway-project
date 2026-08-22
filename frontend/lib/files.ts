/**
 * Attachments, on the client side: what may be uploaded, and how to say what
 * happened to it once it was.
 *
 * Two halves, and they belong together because they are the two ends of one
 * story — the gate a file passes through before it is sent, and the disclosure
 * that comes back saying which model actually read it.
 *
 * **The gate here is a courtesy, not the rule.** `app/api/v1/files.py` sniffs
 * the magic bytes and counts them as they arrive; a browser's `File.type` is
 * whatever the OS guessed from the extension, and `File.size` is trustworthy but
 * the cap is the gateway's to enforce. So this rejects the obvious cases before
 * spending a round trip on them — a 40MB video, a `.zip` — and everything it
 * lets through is still the gateway's to refuse. When it does, `ErrorState`
 * renders the 413 or the 415 as itself rather than as "that didn't work", which
 * is what keeps the two layers from disagreeing in a way the user has to
 * decode.
 */

import { GatewayError, NetworkError } from "./api";
import type { ExtractionTier, FileRefBlock } from "./types";

/**
 * An attachment that made it onto a message: the gateway's `FileOut` reshaped
 * into exactly the `file_ref` block the server will store for it.
 *
 * Carrying all four fields rather than only the hash is what lets the optimistic
 * transcript render the same chip the stored row will render a moment later —
 * `applyOptimisticTurn` builds `FileRefBlock`s straight out of these.
 */
export type SentAttachment = Omit<FileRefBlock, "type">;

/**
 * Mirrors `Settings.FILE_MAX_BYTES`. Duplicated deliberately and knowingly:
 * there is no code generation across this boundary, and the alternative — no
 * client-side cap at all — means uploading ten megabytes to be told no. If the
 * two ever drift, the gateway wins and the user sees a 413 with its own copy.
 */
export const MAX_FILE_BYTES = 10_000_000;

/** `InputMessage.file_refs` is `max_length=4` (app/schemas/chat.py). */
export const MAX_ATTACHMENTS = 4;

/**
 * The allowlist, mirroring `files.sniff_mime`'s range — which *is* the server's
 * allowlist, because a format it cannot identify from its magic bytes is a
 * format no tier of the perception lane can read.
 *
 * Extensions ride along because a browser hands `File.type` an empty string
 * often enough (a file dragged out of an archive, some Linux desktops) that
 * refusing on the MIME alone would reject perfectly good PDFs.
 */
const ACCEPTED: { mime: string; extensions: string[]; label: string }[] = [
  { mime: "application/pdf", extensions: [".pdf"], label: "PDF" },
  { mime: "image/png", extensions: [".png"], label: "PNG" },
  { mime: "image/jpeg", extensions: [".jpg", ".jpeg"], label: "JPEG" },
  { mime: "image/webp", extensions: [".webp"], label: "WebP" },
];

/** The `accept` attribute for the file input. Both forms: Safari and Firefox
 *  filter on extensions, Chrome on MIME types, and neither covers the other. */
export const FILE_ACCEPT = ACCEPTED.flatMap((entry) => [entry.mime, ...entry.extensions]).join(",");

/** "PDF, PNG, JPEG or WebP" — for the empty state and the 415 copy. */
export const ACCEPTED_LABEL = (() => {
  const labels = ACCEPTED.map((entry) => entry.label);
  return `${labels.slice(0, -1).join(", ")} or ${labels.at(-1)}`;
})();

/** Base-10, because a file manager says 1.2 MB and so should this. */
export function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

export function isAcceptedFile(file: { name: string; type: string }): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED.some(
    (entry) =>
      entry.mime === file.type || entry.extensions.some((extension) => name.endsWith(extension)),
  );
}

/**
 * Why this file cannot be attached, or `null` if it can.
 *
 * Returns prose rather than a code: it is rendered directly on the chip that
 * failed, next to the filename, and there is no second consumer that would want
 * to branch on it.
 */
export function rejectionFor(file: { name: string; type: string; size: number }): string | null {
  if (!isAcceptedFile(file)) return `Not a supported file type — ${ACCEPTED_LABEL} only.`;
  if (file.size > MAX_FILE_BYTES) {
    return `Too large — ${formatBytes(file.size)}, and the limit is ${formatBytes(MAX_FILE_BYTES)}.`;
  }
  if (file.size === 0) return "That file is empty.";
  return null;
}

// --------------------------------------------------------------------------- //
// The other end: how the document reached the model
// --------------------------------------------------------------------------- //
export type TierDescription = {
  /** Chip-sized. Sits in the `ModelIndicator`'s row, so it has to be short. */
  label: string;
  /** The full sentence, behind the tooltip and read out to assistive tech. */
  detail: string;
  /** `warn` is reserved for a reading nobody should trust — tier 3 (D25). */
  tone: "neutral" | "warn";
};

/**
 * `extraction_tier` → the disclosure under the answer.
 *
 * This is `served_by`'s discipline applied to perception: the gateway does not
 * get to be vague about *how* a document was read any more than about which
 * model answered. The four tiers are four genuinely different guarantees.
 *
 * `native` is the one that can name its reader — the model that read the file is
 * the model that answered, and it is already named two chips to the left, so the
 * label does not repeat it and the detail does. `llm` cannot: the perception
 * slot is internal, its candidates are chosen inside the lane, and no field on
 * the wire carries which one won. Saying "another model" is what we actually
 * know; naming a guess would be the one thing this indicator must never do.
 *
 * **`degraded` sharpens two of the four**, because the tier alone does not say
 * enough. Tier 3 is either a PDF's own text layer (`medium`) or OCR over a scan
 * (`low`), and calling the first one OCR would be plainly wrong; tier 0 replays
 * a stored reading that may have come from either a model or the local tier, and
 * only `degraded` distinguishes them. `local` is warn-toned either way — no
 * model saw that document — while `cache` is warn-toned only when the reading it
 * replayed was one nobody vouches for.
 */
export function describeTier(
  tier: ExtractionTier,
  answeringModel: string,
  degraded = false,
): TierDescription {
  switch (tier) {
    case "native":
      return {
        label: "read directly",
        detail: `${answeringModel} was handed the file itself and read it natively.`,
        tone: "neutral",
      };
    case "llm":
      return {
        label: "read by another model",
        detail:
          `${answeringModel} cannot open this file, so a model that can was asked to ` +
          "describe it first, and the description was passed along.",
        tone: "neutral",
      };
    case "cache":
      return degraded
        ? {
            label: "read earlier, locally",
            detail:
              "A stored reading of this document was reused, and it was one the gateway " +
              "produced itself rather than one a model produced. Treat the answer with " +
              "more caution than usual.",
            tone: "warn",
          }
        : {
            label: "read earlier",
            detail:
              "A stored reading of this document was reused, so nothing was sent to a " +
              "provider to read it again.",
            tone: "neutral",
          };
    case "local":
      return degraded
        ? {
            label: "read by local OCR",
            detail:
              "No model was available to read this document, so the gateway ran OCR over " +
              "it and answered from whatever that recovered. Treat the answer with more " +
              "suspicion than usual.",
            tone: "warn",
          }
        : {
            label: "read locally",
            detail:
              "No model read this document. The gateway pulled the text out of the file " +
              "itself, which works well for a PDF that carries one and not at all for a scan.",
            tone: "warn",
          };
  }
}


// --------------------------------------------------------------------------- //
// When the gateway refuses a file
// --------------------------------------------------------------------------- //
/**
 * The four attachment-shaped failures, in words.
 *
 * Keyed on `error.code` and nothing else, per the envelope's own rule: the code
 * is the stable machine-readable half and `message` is prose that may be
 * rewritten. Shared by `ErrorState`'s full card and by the one-line reason on a
 * failed chip, so the two never describe the same 415 differently.
 */
export const ATTACHMENT_ERROR_COPY: Record<string, { title: string; detail: string }> = {
  payload_too_large: {
    title: "That file is too large",
    detail: `The gateway accepts files up to ${formatBytes(MAX_FILE_BYTES)}.`,
  },
  unsupported_media_type: {
    title: "That file type isn't supported",
    // The gateway decides on the *sniffed* bytes, not the extension, so a file
    // renamed to `.pdf` lands here — and the copy has to make that make sense.
    detail: `Only ${ACCEPTED_LABEL} files can be read, whatever the file is named.`,
  },
  file_not_found: {
    title: "That file isn't available",
    detail: "It may have been uploaded by another account. Try attaching it again.",
  },
  file_unreadable: {
    title: "The gateway couldn't read that file",
    detail:
      "Every way it has of reading a document came back with nothing — so rather than " +
      "answer about a file nobody read, it stopped. A clearer scan may work.",
  },
};

/** Chip-sized: one sentence, no title, for the failure line under a filename. */
export function uploadFailureReason(error: unknown): string {
  if (error instanceof NetworkError) return "Couldn't reach the gateway.";
  if (error instanceof GatewayError) {
    return ATTACHMENT_ERROR_COPY[error.code]?.detail ?? error.message;
  }
  return "Upload failed.";
}
