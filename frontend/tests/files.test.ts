/**
 * `lib/files.ts` — the client-side attachment gate, and the disclosure that
 * comes back.
 *
 * Two things are worth pinning here, and they are pinned against the *server*
 * rather than against this module's own constants:
 *
 * 1. **The gate agrees with `app/api/v1/files.py`.** The allowlist mirrors
 *    `sniff_mime`'s range and the cap mirrors `FILE_MAX_BYTES`; there is no code
 *    generation across that boundary, so a test that states the numbers out loud
 *    is what makes a drift visible instead of silent.
 * 2. **The tier copy never claims more than the wire carries.** `llm` cannot
 *    name the model that read the document — no field on the response says
 *    which perception candidate won — and a disclosure component that invented
 *    one would be worse than one that says nothing.
 */

import { describe, expect, it } from "vitest";

import { GatewayError, NetworkError } from "@/lib/api";
import {
  ACCEPTED_LABEL,
  FILE_ACCEPT,
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  describeTier,
  formatBytes,
  isAcceptedFile,
  rejectionFor,
  uploadFailureReason,
} from "@/lib/files";

const file = (name: string, type: string, size: number) => ({ name, type, size });

describe("the allowlist mirrors the gateway's sniffer", () => {
  it("accepts exactly the four formats `sniff_mime` can identify", () => {
    expect(isAcceptedFile(file("q3.pdf", "application/pdf", 10))).toBe(true);
    expect(isAcceptedFile(file("chart.png", "image/png", 10))).toBe(true);
    expect(isAcceptedFile(file("photo.jpg", "image/jpeg", 10))).toBe(true);
    expect(isAcceptedFile(file("shot.webp", "image/webp", 10))).toBe(true);
  });

  it("refuses a format no tier of the perception lane could read", () => {
    expect(isAcceptedFile(file("notes.docx", "application/vnd.openxml", 10))).toBe(false);
    expect(isAcceptedFile(file("archive.zip", "application/zip", 10))).toBe(false);
  });

  it("falls back to the extension when the browser reports no type", () => {
    // Routinely empty for a file dragged out of an archive, and on some Linux
    // desktops. Refusing on the MIME alone would reject a perfectly good PDF.
    expect(isAcceptedFile(file("q3.pdf", "", 10))).toBe(true);
  });

  it("offers both MIME types and extensions to the file picker", () => {
    // Safari and Firefox filter on extensions, Chrome on MIME types, and
    // neither covers the other.
    expect(FILE_ACCEPT).toContain("application/pdf");
    expect(FILE_ACCEPT).toContain(".pdf");
  });
});

describe("rejectionFor — the round trip that never happens", () => {
  it("passes a file the gateway would accept", () => {
    expect(rejectionFor(file("q3.pdf", "application/pdf", 1_200_000))).toBeNull();
  });

  it("refuses an oversized file, naming both numbers", () => {
    const reason = rejectionFor(file("huge.pdf", "application/pdf", MAX_FILE_BYTES + 1));

    expect(reason).toContain("Too large");
    // The user has to be able to act on this: how big it is, and how big it may be.
    expect(reason).toContain(formatBytes(MAX_FILE_BYTES));
  });

  it("refuses a disallowed type, naming what is allowed", () => {
    expect(rejectionFor(file("clip.mp4", "video/mp4", 500))).toContain(ACCEPTED_LABEL);
  });

  it("refuses an empty file", () => {
    // `POST /v1/files` answers a zero-byte body with `empty_file`; catching it
    // here saves a round trip to be told something obvious.
    expect(rejectionFor(file("empty.pdf", "application/pdf", 0))).toBe("That file is empty.");
  });

  it("agrees with the server's own limits", () => {
    expect(MAX_FILE_BYTES).toBe(10_000_000); // Settings.FILE_MAX_BYTES
    expect(MAX_ATTACHMENTS).toBe(4); // InputMessage.file_refs, max_length=4
  });
});

describe("formatBytes", () => {
  it("reads like a file manager", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(12_800)).toBe("13 KB");
    expect(formatBytes(1_240_000)).toBe("1.2 MB");
  });
});

describe("describeTier — the perception disclosure", () => {
  it("names the answering model only for the tier that actually read the file", () => {
    expect(describeTier("native", "Gemini 3.6 Flash").detail).toContain("Gemini 3.6 Flash");
  });

  it("does not invent a reader for an extraction it cannot identify", () => {
    // The perception slot is internal and nothing on the wire says which of its
    // candidates won. "Another model" is what we know.
    const llm = describeTier("llm", "GPT-OSS 120B");

    expect(llm.label).toBe("read by another model");
    expect(llm.tone).toBe("neutral");
  });

  it("warns for anything no model actually read", () => {
    // Tier 3 is warn-toned whether or not the answer came back `degraded`: a
    // text-layer read is more trustworthy than OCR, but no model saw the file
    // either way.
    expect(describeTier("local", "GPT-OSS 120B").tone).toBe("warn");
    expect(describeTier("local", "GPT-OSS 120B", true).tone).toBe("warn");
    expect(describeTier("native", "GPT-OSS 120B").tone).toBe("neutral");
    expect(describeTier("llm", "GPT-OSS 120B").tone).toBe("neutral");
  });

  it("does not call a text-layer read OCR", () => {
    // Tier 3 is a PDF's own text layer *or* OCR over a scan, and only
    // `degraded` tells them apart. Labelling the first one OCR is simply wrong.
    expect(describeTier("local", "GPT-OSS 120B", false).label).toBe("read locally");
    expect(describeTier("local", "GPT-OSS 120B", true).label).toBe("read by local OCR");
  });

  it("warns on a replayed reading only when that reading was a local one", () => {
    // Tier 0 says a stored extraction was reused; it does not say which tier
    // wrote it, and `degraded` is the only thing that does.
    expect(describeTier("cache", "GPT-OSS 120B", false).tone).toBe("neutral");
    expect(describeTier("cache", "GPT-OSS 120B", true).tone).toBe("warn");
  });
});

describe("uploadFailureReason", () => {
  it("renders a 413 and a 415 as themselves, not as a generic failure", () => {
    const tooLarge = new GatewayError({
      status: 413,
      code: "payload_too_large",
      message: "The uploaded file is larger than this gateway accepts.",
      requestId: null,
    });
    const unsupported = new GatewayError({
      status: 415,
      code: "unsupported_media_type",
      message: "That file type is not one the gateway can read.",
      requestId: null,
    });

    expect(uploadFailureReason(tooLarge)).toContain(formatBytes(MAX_FILE_BYTES));
    expect(uploadFailureReason(unsupported)).toContain(ACCEPTED_LABEL);
  });

  it("falls through to the gateway's own prose for a code it has no copy for", () => {
    const other = new GatewayError({
      status: 500,
      code: "internal_error",
      message: "Something specific went wrong.",
      requestId: "abc",
    });

    expect(uploadFailureReason(other)).toBe("Something specific went wrong.");
  });

  it("distinguishes a gateway that said no from one that was never reached", () => {
    expect(uploadFailureReason(new NetworkError())).toBe("Couldn't reach the gateway.");
  });
});
