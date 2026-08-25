/**
 * D33's client half: a thread reopens on the slot you last used.
 *
 * The bug this pins is small and completely invisible in the API — pick `fast`
 * on turn nine, reload, and the composer is silently back on `auto`, so turn
 * ten goes somewhere you did not choose. The server has stored
 * `preferred_slot` since Phase 1 and updated it per turn since Step 5; this is
 * the half that reads it.
 *
 * Written against the observable behaviour rather than the mechanism: what the
 * picker shows, and — the thing that actually matters — which slot
 * `useSendMessage` was handed, because that is the value the next request
 * rides on.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationView } from "@/components/ConversationView";
import type { ConversationDetail, ModelEntry, ModelsResponse } from "@/lib/types";

const { useConversation, useModels, useSendMessage } = vi.hoisted(() => ({
  useConversation: vi.fn(),
  useModels: vi.fn(),
  useSendMessage: vi.fn(),
}));

// Partial mock: `useAttachments` and `DEFAULT_SLOT` stay real, because the
// component under test is only interesting when the rest of its wiring is.
vi.mock("@/lib/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/hooks")>()),
  useConversation,
  useModels,
  useSendMessage,
}));

const CONVERSATION_ID = "8b0d1f6e-0000-4000-8000-000000000000";

/** `GET /v1/models` as `config/providers.yaml` actually reports it: `auto`
 *  plus the two public slots. The picker only offers what this carries, so a
 *  test that changed the value to a slot missing here would be selecting
 *  nothing. */
const MODELS: ModelsResponse = {
  object: "list",
  data: (["auto", "general", "fast"] as const).map(
    (id): ModelEntry => ({
      id,
      object: "model",
      created: 0,
      owned_by: null,
      status: "available",
      resets_at: null,
      description: "",
      candidates: [],
    }),
  ),
};

function conversation(overrides: Partial<ConversationDetail> = {}): ConversationDetail {
  return {
    id: CONVERSATION_ID,
    title: null,
    preferred_slot: "fast",
    pinned_model: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    messages: [],
    ...overrides,
  };
}

/** What `useConversation` returns, in one of its three interesting states. */
function loaded(detail: ConversationDetail | undefined, isLoading = false) {
  useConversation.mockReturnValue({
    conversation: detail,
    error: undefined,
    isLoading,
    mutate: vi.fn(),
  });
}

/** The slot the composer is currently showing. */
function pickerValue(): string {
  return screen.getByRole("combobox", { name: /Model/ }).textContent?.trim() ?? "";
}

/** Pick a slot the way a user does: open the picker, click the row. */
function choose(slot: string): void {
  fireEvent.click(screen.getByRole("combobox", { name: /Model/ }));
  fireEvent.click(screen.getByRole("option", { name: slot }));
}

/** The slot the *next send* would actually use — the point of the whole feature. */
function slotHandedToSend(): string {
  const calls = useSendMessage.mock.calls;
  return calls.at(-1)?.[1] as string;
}

beforeEach(() => {
  vi.clearAllMocks();
  useModels.mockReturnValue({ models: MODELS, error: undefined, isLoading: false });
  useSendMessage.mockReturnValue({
    pending: null,
    send: vi.fn(),
    stop: vi.fn(),
    retry: vi.fn(),
    keepPartial: vi.fn(),
    dismiss: vi.fn(),
  });
});

describe("the composer opens on the thread's stored slot", () => {
  it("adopts `preferred_slot` once the conversation has loaded", () => {
    loaded(conversation({ preferred_slot: "fast" }));

    render(<ConversationView conversationId={CONVERSATION_ID} />);

    expect(pickerValue()).toBe("fast");
    expect(slotHandedToSend()).toBe("fast");
  });

  it("falls back to the default while the conversation is still in flight", () => {
    // First paint happens before the fetch resolves. `auto` is the honest
    // placeholder — the alternative is a picker that renders empty, or one
    // that guesses.
    loaded(undefined, true);

    render(<ConversationView conversationId={CONVERSATION_ID} />);

    expect(pickerValue()).toBe("auto");
    expect(slotHandedToSend()).toBe("auto");
  });

  it("adopts the stored slot when the fetch lands after first paint", () => {
    // Trap 10, the straightforward half: a value captured on the first render
    // would be `undefined` forever, so the picker would never move.
    loaded(undefined, true);
    const view = render(<ConversationView conversationId={CONVERSATION_ID} />);
    expect(pickerValue()).toBe("auto");

    loaded(conversation({ preferred_slot: "general" }));
    view.rerender(<ConversationView conversationId={CONVERSATION_ID} />);

    expect(pickerValue()).toBe("general");
    expect(slotHandedToSend()).toBe("general");
  });

  it("does not stomp a slot the user picked while the fetch was in flight", () => {
    // Trap 10, the half that actually bites: the user is faster than the
    // network, picks `general`, and the response arrives saying `fast`. Their
    // choice is the newer fact and has to win.
    loaded(undefined, true);
    const view = render(<ConversationView conversationId={CONVERSATION_ID} />);

    choose("general");
    expect(pickerValue()).toBe("general");

    loaded(conversation({ preferred_slot: "fast" }));
    view.rerender(<ConversationView conversationId={CONVERSATION_ID} />);

    expect(pickerValue()).toBe("general");
    expect(slotHandedToSend()).toBe("general");
  });

  it("lets a pick override the stored preference after it has loaded", () => {
    // A preference, not a pin: one click overrides it, always. The server will
    // record the new slot on the next turn and the thread will reopen on it.
    loaded(conversation({ preferred_slot: "fast" }));
    render(<ConversationView conversationId={CONVERSATION_ID} />);
    expect(pickerValue()).toBe("fast");

    choose("general");

    expect(pickerValue()).toBe("general");
    expect(slotHandedToSend()).toBe("general");
  });

  it("re-reads the preference when the view moves to another thread", () => {
    // The pick is held against the id it was made under, so navigating to a
    // different conversation cannot carry one thread's choice into another.
    loaded(conversation({ preferred_slot: "fast" }));
    const view = render(<ConversationView conversationId={CONVERSATION_ID} />);
    choose("general");
    expect(pickerValue()).toBe("general");

    const other = "8b0d1f6e-0000-4000-8000-00000000000f";
    loaded(conversation({ id: other, preferred_slot: "fast" }));
    view.rerender(<ConversationView conversationId={other} />);

    expect(pickerValue()).toBe("fast");
    expect(slotHandedToSend()).toBe("fast");
  });
});
