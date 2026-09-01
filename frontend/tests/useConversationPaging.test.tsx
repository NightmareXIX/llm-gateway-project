/**
 * D48's client half: older pages live outside the SWR cache, and landing one
 * must not move the viewport.
 *
 * Two failures this pins, both invisible from the server side. The first is
 * trap 12: if older pages were written into the head's SWR key, every
 * `globalMutate(conversationKey(id))` in `hooks.ts` — one fires after every
 * completed turn — would drop them, and a thread getting *shorter* after you
 * send a message reads as data loss. The second is trap 13: prepending rows
 * above the viewport without anchoring throws the reader to the top of the
 * page that just arrived, which makes scrolling back through a long thread
 * unusable rather than untidy.
 *
 * The hook is exercised through `renderHook` against a mocked `useSWR`, so the
 * head can be replaced mid-fetch the way a real revalidation replaces it.
 */

import { useRef } from "react";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MessageList } from "@/components/MessageList";
import { useConversation } from "@/lib/hooks";
import type { ConversationDetail, Message, MessagePage } from "@/lib/types";

const { useSWR, globalMutate, fetchMessagePage } = vi.hoisted(() => ({
  useSWR: vi.fn(),
  globalMutate: vi.fn(async () => undefined),
  fetchMessagePage: vi.fn(),
}));

vi.mock("swr", () => ({ default: useSWR, mutate: globalMutate }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { fetchMessagePage },
  swrFetcher: vi.fn(),
}));

const ID = "8b0d1f6e-0000-4000-8000-000000000000";
const OTHER_ID = "8b0d1f6e-0000-4000-8000-00000000000f";

function message(seq: number): Message {
  return {
    id: `m${seq}`,
    seq,
    role: seq % 2 === 0 ? "user" : "assistant",
    content: [{ type: "text", text: `message ${seq}` }],
    meta: {},
    created_at: "2026-01-01T00:00:00Z",
  };
}

function detail(overrides: Partial<ConversationDetail> = {}): ConversationDetail {
  return {
    id: ID,
    title: null,
    preferred_slot: "auto",
    pinned_model: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    messages: [message(100), message(101)],
    has_more: true,
    next_before_seq: 100,
    ...overrides,
  };
}

/** What the head SWR entry currently holds. */
function head(data: ConversationDetail | undefined) {
  useSWR.mockReturnValue({ data, error: undefined, isLoading: false, mutate: vi.fn() });
}

function page(messages: Message[], hasMore: boolean, next: number | null): MessagePage {
  return { messages, has_more: hasMore, next_before_seq: next };
}

const ids = (messages: Message[]) => messages.map((message) => message.id);

beforeEach(() => {
  vi.clearAllMocks();
  head(detail());
});

describe("useConversation merges older pages with the head", () => {
  it("prepends a page and leaves the head untouched", async () => {
    fetchMessagePage.mockResolvedValueOnce(page([message(98), message(99)], true, 98));
    const { result } = renderHook(() => useConversation(ID));

    expect(ids(result.current.messages)).toEqual(["m100", "m101"]);
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadOlder();
    });

    // The cursor came from the head response, not from arithmetic on a seq.
    expect(fetchMessagePage).toHaveBeenCalledWith(ID, 100);
    expect(ids(result.current.messages)).toEqual(["m98", "m99", "m100", "m101"]);
    // The head SWR entry is never written to — that is the whole reason older
    // pages are component state (trap 12).
    expect(globalMutate).not.toHaveBeenCalled();
  });

  it("walks back page by page, each fetch using the previous page's cursor", async () => {
    fetchMessagePage
      .mockResolvedValueOnce(page([message(98), message(99)], true, 98))
      .mockResolvedValueOnce(page([message(96), message(97)], false, null));
    const { result } = renderHook(() => useConversation(ID));

    await act(async () => {
      await result.current.loadOlder();
    });
    await act(async () => {
      await result.current.loadOlder();
    });

    expect(fetchMessagePage.mock.calls).toEqual([
      [ID, 100],
      [ID, 98],
    ]);
    expect(ids(result.current.messages)).toEqual(["m96", "m97", "m98", "m99", "m100", "m101"]);
    expect(result.current.hasMore).toBe(false);
  });

  it("stops offering more once the last page has landed", async () => {
    fetchMessagePage.mockResolvedValueOnce(page([message(99)], false, null));
    const { result } = renderHook(() => useConversation(ID));

    await act(async () => {
      await result.current.loadOlder();
    });

    expect(result.current.hasMore).toBe(false);

    await act(async () => {
      await result.current.loadOlder();
    });

    expect(fetchMessagePage).toHaveBeenCalledTimes(1);
  });

  it("shows a row that appears in both exactly once, keeping the head's copy", async () => {
    // A page boundary and a concurrent write can disagree; two React children
    // with the same key is a rendering bug, not a data question.
    fetchMessagePage.mockResolvedValueOnce(page([message(99), message(100)], true, 99));
    const { result } = renderHook(() => useConversation(ID));

    await act(async () => {
      await result.current.loadOlder();
    });

    expect(ids(result.current.messages)).toEqual(["m99", "m100", "m101"]);
  });

  it("renders a thread under one page exactly as it did before pagination", () => {
    head(detail({ has_more: false, next_before_seq: null }));
    const { result } = renderHook(() => useConversation(ID));

    expect(ids(result.current.messages)).toEqual(["m100", "m101"]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.isLoadingOlder).toBe(false);
  });

  it("treats a server that sends neither field as one page, not as unknown", () => {
    // Both fields are optional on the wire: a client build can be newer than
    // the gateway it talks to, and the honest reading of silence is "nothing
    // older" rather than a fetch for a cursor nobody supplied.
    head(detail({ has_more: undefined, next_before_seq: undefined }));
    const { result } = renderHook(() => useConversation(ID));

    expect(result.current.hasMore).toBe(false);
    void result.current.loadOlder();
    expect(fetchMessagePage).not.toHaveBeenCalled();
  });
});

describe("loadOlder is guarded", () => {
  it("collapses a burst of calls into one request", async () => {
    // `onScroll` fires dozens of times per gesture. Without the ref guard,
    // every one of them would request the same page — and page four would be
    // requested before page two arrived.
    let resolvePage: (value: MessagePage) => void = () => {};
    fetchMessagePage.mockReturnValueOnce(
      new Promise<MessagePage>((resolve) => {
        resolvePage = resolve;
      }),
    );
    const { result } = renderHook(() => useConversation(ID));

    let inFlight: Promise<void> = Promise.resolve();
    act(() => {
      inFlight = Promise.all([
        result.current.loadOlder(),
        result.current.loadOlder(),
        result.current.loadOlder(),
      ]).then(() => undefined);
    });

    expect(fetchMessagePage).toHaveBeenCalledTimes(1);
    expect(result.current.isLoadingOlder).toBe(true);

    await act(async () => {
      resolvePage(page([message(98), message(99)], true, 98));
      await inFlight;
    });

    expect(ids(result.current.messages)).toEqual(["m98", "m99", "m100", "m101"]);
    expect(result.current.isLoadingOlder).toBe(false);
  });

  it("releases the guard when a page fetch fails, so the retry can run", async () => {
    // A page that did not arrive changes nothing on screen and moves no
    // cursor, so it is swallowed rather than rethrown — the trigger coming
    // back enabled is the retry, and a rejection would be unhandled at both
    // `void loadOlder()` call sites.
    fetchMessagePage
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(page([message(99)], false, null));
    const { result } = renderHook(() => useConversation(ID));

    await act(async () => {
      await expect(result.current.loadOlder()).resolves.toBeUndefined();
    });
    expect(ids(result.current.messages)).toEqual(["m100", "m101"]);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.isLoadingOlder).toBe(false);
    await act(async () => {
      await result.current.loadOlder();
    });

    expect(fetchMessagePage).toHaveBeenCalledTimes(2);
    expect(ids(result.current.messages)).toEqual(["m99", "m100", "m101"]);
  });

  it("keeps both halves when a turn lands mid-fetch", async () => {
    // The race D48's two routes exist to prevent, asserted directly: an
    // optimistic turn replaces the head while an older page is in flight.
    // Neither may eat the other.
    let resolvePage: (value: MessagePage) => void = () => {};
    fetchMessagePage.mockReturnValueOnce(
      new Promise<MessagePage>((resolve) => {
        resolvePage = resolve;
      }),
    );
    const { result, rerender } = renderHook(() => useConversation(ID));

    let inFlight: Promise<void> = Promise.resolve();
    act(() => {
      inFlight = result.current.loadOlder();
    });

    // The turn: `applyOptimisticTurn` rewrites the head SWR entry, and SWR
    // hands the hook the new value on its next render.
    head(detail({ messages: [message(100), message(101), message(102), message(103)] }));
    rerender();

    await act(async () => {
      resolvePage(page([message(98), message(99)], true, 98));
      await inFlight;
    });

    expect(ids(result.current.messages)).toEqual([
      "m98",
      "m99",
      "m100",
      "m101",
      "m102",
      "m103",
    ]);
  });

  it("drops another thread's pages when the view moves", async () => {
    fetchMessagePage.mockResolvedValueOnce(page([message(98), message(99)], true, 98));
    const { result, rerender } = renderHook(({ id }) => useConversation(id), {
      initialProps: { id: ID },
    });

    await act(async () => {
      await result.current.loadOlder();
    });
    expect(ids(result.current.messages)).toHaveLength(4);

    head(detail({ id: OTHER_ID, messages: [message(1)], has_more: false, next_before_seq: null }));
    rerender({ id: OTHER_ID });

    expect(ids(result.current.messages)).toEqual(["m1"]);
    expect(result.current.hasMore).toBe(false);
  });
});

// --------------------------------------------------------------------------- //
// The rendering half: the trigger, and the scroll anchoring (trap 13)
// --------------------------------------------------------------------------- //
/** A scrolling container whose geometry the test controls — jsdom performs no
 *  layout, so `scrollHeight` is otherwise always zero and the anchoring
 *  arithmetic would have nothing to work with. */
function Scroller({
  messages,
  hasMore = false,
  isLoadingOlder = false,
  onLoadOlder,
}: {
  messages: Message[];
  hasMore?: boolean;
  isLoadingOlder?: boolean;
  onLoadOlder?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div ref={ref} data-testid="scroller">
      <MessageList
        messages={messages}
        pending={null}
        onRetry={() => {}}
        onDismiss={() => {}}
        scrollContainerRef={ref}
        hasMore={hasMore}
        isLoadingOlder={isLoadingOlder}
        onLoadOlder={onLoadOlder}
      />
    </div>
  );
}

/** Give the container a settable `scrollTop` and a scriptable `scrollHeight`. */
function instrument(element: HTMLElement, geometry: { scrollHeight: number; scrollTop: number }) {
  Object.defineProperty(element, "scrollHeight", {
    configurable: true,
    get: () => geometry.scrollHeight,
  });
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    get: () => geometry.scrollTop,
    set: (value: number) => {
      geometry.scrollTop = value;
    },
  });
}

describe("MessageList's older-page affordances", () => {
  beforeEach(() => {
    // jsdom implements no scrolling; the auto-scroll effect calls this.
    Element.prototype.scrollTo = vi.fn();
  });

  it("offers no trigger when there is nothing older", () => {
    render(<Scroller messages={[message(100), message(101)]} />);

    expect(screen.queryByRole("button", { name: /earlier messages/i })).toBeNull();
  });

  it("offers one when there is, and asks for exactly one page per click", () => {
    const onLoadOlder = vi.fn();
    render(<Scroller messages={[message(100)]} hasMore onLoadOlder={onLoadOlder} />);

    fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it("disables the trigger while a page is in flight", () => {
    const onLoadOlder = vi.fn();
    render(<Scroller messages={[message(100)]} hasMore isLoadingOlder onLoadOlder={onLoadOlder} />);

    const button = screen.getByRole("button", { name: /Loading earlier messages/ });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onLoadOlder).not.toHaveBeenCalled();
  });

  it("holds the viewport still when a page lands above it", () => {
    const geometry = { scrollHeight: 0, scrollTop: 0 };
    const view = render(<Scroller messages={[message(100), message(101)]} hasMore />);
    const scroller = screen.getByTestId("scroller");
    instrument(scroller, geometry);

    // Settle the anchor at the current geometry: two turns tall, scrolled to
    // the top, which is where the trigger fires.
    geometry.scrollHeight = 1000;
    view.rerender(<Scroller messages={[message(100), message(101)]} hasMore />);
    geometry.scrollTop = 0;

    // The page lands: 500px of history above what the reader was looking at.
    geometry.scrollHeight = 1500;
    view.rerender(<Scroller messages={[message(98), message(99), message(100), message(101)]} />);

    expect(geometry.scrollTop).toBe(500);
  });

  it("does not move the viewport when a turn is appended at the bottom", () => {
    const geometry = { scrollHeight: 0, scrollTop: 0 };
    const view = render(<Scroller messages={[message(100), message(101)]} />);
    const scroller = screen.getByTestId("scroller");
    instrument(scroller, geometry);

    geometry.scrollHeight = 1000;
    view.rerender(<Scroller messages={[message(100), message(101)]} />);
    geometry.scrollTop = 400;

    geometry.scrollHeight = 1500;
    view.rerender(<Scroller messages={[message(100), message(101), message(102)]} />);

    // Untouched by the anchoring — the first row did not move. Following the
    // new answer to the bottom is the other effect's job, and it is the one
    // keyed on the *last* message.
    expect(geometry.scrollTop).toBe(400);
  });
});
