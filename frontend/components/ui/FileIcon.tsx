import { cn } from "@/lib/cn";

/**
 * A document or a picture, by MIME type.
 *
 * Two glyphs and no more. The allowlist is PDF, PNG, JPEG and WebP — three of
 * which are the same thing to a reader — so a per-format icon set would be
 * three near-identical rectangles pretending to carry information. What the
 * shape actually has to say is which of the two kinds of thing this is, because
 * that is what changes how the perception lane will read it.
 *
 * Purely decorative: every call site already renders the filename next to it,
 * so the glyph is `aria-hidden` and adds nothing to the accessible name.
 */
export function FileIcon({ mime, className }: { mime?: string; className?: string }) {
  const isImage = mime?.startsWith("image/") ?? false;

  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={cn("shrink-0", className)}
      aria-hidden
      focusable="false"
    >
      {isImage ? (
        <>
          <rect
            x="3"
            y="4"
            width="18"
            height="16"
            rx="2"
            stroke="currentColor"
            strokeWidth="1.6"
          />
          <circle cx="8.75" cy="9.25" r="1.4" fill="currentColor" />
          <path
            d="M4 17.5 9 12.5l3.5 3.5L15.5 13l4.5 4.5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </>
      ) : (
        <>
          <path
            d="M14 3v5h5M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
          <path
            d="M8.75 13h6.5M8.75 16.5h4"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </>
      )}
    </svg>
  );
}
