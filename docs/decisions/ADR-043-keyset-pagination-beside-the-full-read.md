# ADR-043 — Keyset pagination is a second function and a second route, beside an untouched full read

**Status:** accepted · Phase 7, Steps 7–8 · 2026-09-01
**Implements:** `phase7.md` §3 D48 and §3 D51's client half
**Relates to:** [ADR-033](ADR-033-truncation-disclosed-and-uncached.md) (D4's fitting step, the reason
the full read exists), [ADR-023](ADR-023-exact-cache-identity-and-scope.md) (cache identity, the
analogous "what makes two reads the same" question on the client)

## Context

`GET /v1/conversations/{id}` returned every message in the thread. That is fine for a demo thread and
untenable for a real one: a 300-message conversation is a multi-hundred-kilobyte JSON document
re-fetched after every turn, because SWR revalidates the detail key on every send.

The obvious fix — add `before_seq` and `limit` to the existing route and page it — is the one this ADR
rejects, and the reason is a client-side cache invariant rather than a server-side one.

## Decision

**Two routes, two repo functions, and `list_for_conversation` is not touched.**

- `GET /v1/conversations/{id}` keeps its URL and its shape and now returns the **newest** page
  (default 50, oldest-first within the page) plus two additive fields, `has_more: bool` and
  `next_before_seq: int | null`.
- `GET /v1/conversations/{id}/messages?before_seq=&limit=` is a **new** route serving older pages.
- `db/repo/messages.py::list_page_for_conversation` is a **new** function returning a `MessagePage`,
  keyset on `seq` (`WHERE seq < :before_seq ORDER BY seq DESC LIMIT :n+1`), ownership-scoped by the
  same `Conversation` join the full read already uses, reversed back to oldest-first before returning.
  It fetches `limit + 1` rows to answer `has_more` without a second `COUNT`.
- `list_for_conversation` is unmodified, undeprecated, and **not called by the paginated route**.
  `git diff` shows only pure addition after its closing line.

On the client, `usageKey`-style per-page SWR entries are explicitly *not* used: **older pages live in
component state and are never written into the head SWR key.**

## Why

**The two reads have two cache lifetimes, and that is what makes them two resources.** The detail
route is "this thread as it stands now" — mutated optimistically on every send and revalidated after
every completed turn. A page of old messages is immutable history. One URL with a `before_seq`
parameter makes the head page and page 4 the same cache entry under different arguments, and that is
exactly how an optimistic append ends up prepended to page 4.

**Trap 12 is the sharp edge, and it is a data-loss-shaped bug.** Every `globalMutate(conversationKey(id))`
in `hooks.ts` — one of which fires after every completed turn — would silently drop the older pages a
user had scrolled back through if they had been merged into that key. The symptom is the thread
getting *shorter* after you send a message, which reads to a user as their history being deleted. So
`useConversation` holds older pages as component state keyed against the conversation id they were
loaded for, merges as `[...older, ...head]` de-duplicated by id in the head's favour, and lets
navigation reset the cursor for free with no effect to run and nothing to cancel.

**Keyset, not `OFFSET`.** `seq` is unique and gap-free per conversation by Contract B's invariant 2,
which is exactly the precondition keyset pagination needs. `OFFSET` re-scans the skipped rows on every
page and shifts under a concurrent insert; a thread that grows while you scroll up is the normal case
here, not an edge one.

**`list_for_conversation` stays unpaginated because the render pipeline is not a UI** (trap 1, said in
`development-plan.md` twice, in the repo docstring, and in D48). D4's fitting step needs the *complete*
history to choose what to drop and where to put the omission marker; a page of it moves the truncation
decision somewhere that cannot make it well. Step 7 adds a function; it does not edit one. The
regression test asserting the full read still returns everything is there to make an accidental
"unification" fail loudly.

**Ownership is in the query, not after it.** A non-owner or an unknown id gets an *empty page* from the
repo function rather than someone else's messages; the route resolves ownership with
`conversations.get_owned` first so it can return the "not yours" 404 that the repo layer deliberately
cannot distinguish from "empty". Same split `list_for_conversation` already documents.

**Re-entrancy is guarded by a ref, not by a state flag.** A `setState` is not visible to the
synchronous caller that queued it, so two scroll events in one animation frame would both pass an
`isLoadingOlder` check and fetch the same page twice. The scroll trigger is deliberately undebounced
and collapsed by that guard instead, which also makes the "burst of three `loadOlder` calls issues one
request" test meaningful.

**Scroll anchoring is not polish** (trap 13). Prepending a page moves everything below it down by the
new content's height, and without correction the viewport jumps to a random point in the middle of the
history the user was reading — which makes the feature unusable rather than merely rough. A
`useLayoutEffect` captures `scrollHeight` per commit and, when the first row changed *while still being
present in the list* (which is what tells a prepend apart from a navigation), moves `scrollTop` by the
delta. The auto-scroll-to-bottom effect had to move from `messages.length` to the last message's id for
the same reason: a prepend changes the count and adds nothing at the bottom to follow.

## Consequences

- `has_more`/`next_before_seq` are **optional** on the client's `ConversationDetail` type. A client
  build can be newer than the gateway it talks to, and an absent `has_more` must read as "one page,
  nothing older" rather than as `undefined` — the same shape every wire field added since Phase 5 has.
- A thread under one page renders byte-identically to how it did before this phase, asserted at both
  the repo and component layers.
- Pagination applies to the *read*, not to the render pipeline: a very long thread still costs a full
  history read on every turn. That is a real cost, it is `docs/limitations.md`'s to carry, and the fix
  is summarization (§7 item 2), not a paged render.
- There is no "jump to date" or search-in-thread; the cursor walks backwards from the head only. Adding
  a forward cursor is a second parameter on the same function, not a redesign.
- `next_before_seq` is a `seq`, not an opaque token. It is already stable, already gap-free, and
  already visible in the message payload, so wrapping it would be ceremony.
