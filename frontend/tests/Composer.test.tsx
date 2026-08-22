/**
 * The composer's attachment flow — Phase 4, Step 10.
 *
 * The four things this has to get right, and each of them is a way the feature
 * fails silently rather than loudly:
 *
 * 1. **An oversized or disallowed file is refused without a request.** The whole
 *    point of a client-side gate is the round trip it does not spend, so the
 *    assertion is on `api.uploadFile` never having been called — not on a
 *    message appearing.
 * 2. **The upload happens on selection.** If it happened on send, the hash would
 *    not exist when the message did.
 * 3. **A message cannot be sent referencing a hash that does not exist yet.**
 *    Submitting mid-upload has to wait rather than send an empty `file_refs`.
 * 4. **What leaves through `onSend` is the resolved attachments**, so the turn
 *    and its optimistic bubble carry the same four fields the stored row will.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/Composer";
import type { FileUploadResponse } from "@/lib/types";

const { uploadFile } = vi.hoisted(() => ({ uploadFile: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { uploadFile },
}));

const HASH = "a".repeat(64);

function uploaded(overrides: Partial<FileUploadResponse> = {}): FileUploadResponse {
  return {
    file_hash: HASH,
    filename: "q3.pdf",
    mime: "application/pdf",
    bytes: 1_240_000,
    created_at: "2026-08-22T10:00:00Z",
    deduplicated: false,
    ...overrides,
  };
}

/** A `File` with a size the browser would report. `new File([...])` in jsdom
 *  reports the real byte length, and a 10MB string in a test is 10MB of heap. */
function fileOf(name: string, type: string, size: number): File {
  const file = new File(["x"], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function attach(files: File[]) {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) throw new Error("the composer rendered no file input");
  fireEvent.change(input, { target: { files } });
}

beforeEach(() => vi.clearAllMocks());

describe("client-side rejection", () => {
  it("refuses an oversized file without spending a request", async () => {
    render(<Composer onSend={vi.fn()} pending={false} />);

    attach([fileOf("huge.pdf", "application/pdf", 11_000_000)]);

    expect(await screen.findByText("huge.pdf")).toBeInTheDocument();
    expect(screen.getByText(/Too large/)).toBeInTheDocument();
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it("refuses a file type no tier of the perception lane could read", async () => {
    render(<Composer onSend={vi.fn()} pending={false} />);

    attach([fileOf("clip.mp4", "video/mp4", 4_000)]);

    expect(await screen.findByText(/Not a supported file type/)).toBeInTheDocument();
    expect(uploadFile).not.toHaveBeenCalled();
  });

  it("keeps the refused file on screen with its reason rather than dropping it", async () => {
    // A file that vanishes into a toast gives the user nothing to act on. The
    // chip stays, and removing it is their decision.
    render(<Composer onSend={vi.fn()} pending={false} />);

    attach([fileOf("huge.pdf", "application/pdf", 11_000_000)]);
    expect(await screen.findByText("huge.pdf")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Remove huge\.pdf/ }));

    expect(screen.queryByText("huge.pdf")).not.toBeInTheDocument();
  });
});

describe("the upload, and the message that carries it", () => {
  it("uploads on selection rather than on send", async () => {
    uploadFile.mockResolvedValue(uploaded());
    render(<Composer onSend={vi.fn()} pending={false} />);

    attach([fileOf("q3.pdf", "application/pdf", 1_240_000)]);

    // Nothing has been typed and nothing has been sent, and the hash is already
    // being fetched — which is the entire reason the send costs nothing extra.
    await waitFor(() => expect(uploadFile).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("1.2 MB")).toBeInTheDocument();
  });

  it("hands `onSend` the resolved attachment, not the raw file", async () => {
    uploadFile.mockResolvedValue(uploaded());
    const onSend = vi.fn();
    render(<Composer onSend={onSend} pending={false} />);

    attach([fileOf("q3.pdf", "application/pdf", 1_240_000)]);
    await screen.findByText("1.2 MB");

    fireEvent.change(screen.getByLabelText("Message"), {
      target: { value: "what does this say?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    expect(onSend).toHaveBeenCalledWith("what does this say?", [
      { file_hash: HASH, filename: "q3.pdf", mime: "application/pdf", bytes: 1_240_000 },
    ]);
  });

  it("clears the chips once the message has gone", async () => {
    uploadFile.mockResolvedValue(uploaded());
    render(<Composer onSend={vi.fn()} pending={false} />);

    attach([fileOf("q3.pdf", "application/pdf", 1_240_000)]);
    await screen.findByText("1.2 MB");

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(screen.queryByText("q3.pdf")).not.toBeInTheDocument());
  });

  it("will not send while an upload is still in flight", async () => {
    // Otherwise the turn references a hash the gateway has never seen, and the
    // message fails with a 404 naming a file the user is looking at.
    uploadFile.mockReturnValue(new Promise(() => {}));
    const onSend = vi.fn();
    render(<Composer onSend={onSend} pending={false} />);

    attach([fileOf("q3.pdf", "application/pdf", 1_240_000)]);
    await screen.findByText("Uploading…");

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Waiting for the upload to finish" }));

    expect(onSend).not.toHaveBeenCalled();
  });

  it("reports a gateway refusal on the chip that caused it", async () => {
    const { GatewayError } = await import("@/lib/api");
    uploadFile.mockRejectedValue(
      new GatewayError({
        status: 415,
        code: "unsupported_media_type",
        message: "That file type is not one the gateway can read.",
        requestId: "abc",
      }),
    );
    render(<Composer onSend={vi.fn()} pending={false} />);

    // Named `.pdf` and gated through on the extension — the gateway sniffs the
    // bytes and disagrees, which is exactly what trap 2 is about.
    attach([fileOf("actually-an-exe.pdf", "application/pdf", 4_000)]);

    expect(await screen.findByText(/Only PDF, PNG, JPEG or WebP/)).toBeInTheDocument();
  });
});

describe("the privacy disclosure", () => {
  it("says a file may be sent to another model *before* the message is sent", async () => {
    uploadFile.mockResolvedValue(uploaded());
    render(<Composer onSend={vi.fn()} pending={false} />);

    expect(screen.queryByText(/may be sent/)).not.toBeInTheDocument();

    attach([fileOf("q3.pdf", "application/pdf", 1_240_000)]);

    expect(await screen.findByText(/may be sent/)).toBeInTheDocument();
  });
});
