# Frontend — Phase 1

Next.js (App Router) + Tailwind v4 + the Supabase JS client, talking to the FastAPI gateway.
This is Step 9 of Phase 1: a login page, a conversation sidebar, a message view with a composer,
and `ModelIndicator` — nothing else.

## Running it

```bash
cp .env.local.example .env.local     # fill in the Supabase project + gateway URL
npm install
npm run dev                          # http://localhost:3000
```

The gateway must be up (`make dev` at the repo root, with Postgres from `docker compose up -d`).
The same Supabase project has to back both: the frontend mints the JWT, the gateway verifies it
against that project's JWKS.

| Script | |
|---|---|
| `npm run dev` | Dev server |
| `npm run build` | Production build |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run lint` | ESLint |
| `npm test` | Vitest — the `ModelIndicator` contract suite |

## How it talks to the gateway

The browser never calls the API host directly. Everything goes to `/api/gw/*` on this origin and
`next.config.ts` rewrites it to `GATEWAY_URL`. Same-origin means no preflight, no CORS
configuration to get wrong, and no change to `app/main.py` — which mounts no CORS middleware, by
design. `lib/api.ts` attaches the Supabase access token and parses the gateway's error envelope
into a typed `GatewayError` carrying `code`, `message` and `request_id`.

## `ModelIndicator` is a frozen contract

§1.1 of `doc/reference/contracts-and-phase1.md` specifies what goes under every assistant message,
and this component implements all four rules now even though Phase 1 can only exercise the first:

1. **Always** render `served_by` — model, provider, slot.
2. `substituted` → name the slot that was asked for and couldn't serve.
3. `attempts > 1` → the attempt trail, on hover *and* in a visually-hidden node.
4. `degraded` → say the answer was read with local extraction.

It takes one `Provenance` object. `lib/provenance.ts` builds that from a stored message's `meta`
(`fromMessageMeta`) or from a completion response (`fromCompletion`); Phase 2 adds `fromDoneEvent`
for the SSE `done` event and touches neither the component nor its callers. `tests/` pins the four
rules and the agreement between the two adapters — a message must look identical whether it was
just sent or read back after a refresh.

`lib/sse.ts` declares §1.1's `meta` / `delta` / `restart` / `done` event types and throws from
`openCompletionStream`. Typed signature, deferred body — the frontend's version of the repo rule
that a Phase 2+ seam is never a silently-passing stub.

## Dependency overrides

`npm audit` reports two advisories that reach us only through `next` — `postcss <=8.5.22` and
`sharp <0.35.0`. `npm audit fix --force` resolves them by installing next@16, a major bump we have
no other reason to take, so `package.json` pins the patched transitive versions via `overrides`
instead. `npm audit` is clean. Revisit when the project moves to Next 16 anyway.

## Not built, on purpose

`ModelPicker.tsx`, `FileUpload.tsx` and a settings/BYOK page appear in the repo-structure doc but
are absent here. Phase 1 has no `/v1/models` endpoint, no `POST /v1/files` and no BYOK resolver, and
a control for a capability the backend lacks is worse than its absence. They arrive with the phases
that give them something to do.

## Known Phase 1 rough edge

**Retrying a failed send duplicates the user's turn.** The gateway persists the inbound message
*before* calling the provider (deliberately — a thread that loses what you typed when the provider
fails is worse than one that shows an error next to it), so a resend appends a second copy. Canonical
invariant 3 tolerates consecutive user messages, so this is legal rather than corrupt, and the error
card says so plainly instead of producing a confusing transcript. The fix is a backend one: a
continuation request that carries no new message. Out of scope for Step 9.

**Conversation titles are derived client-side.** `conversations.title` starts null and Phase 1
generates none, so after the first turn the client `PATCH`es the title to the first ~60 characters
of the opening message via the existing rename endpoint.

## Design notes

**Direction: warm editorial.** The primary content is long-form model prose, so the ground is a warm
off-white (`#FAF9F7`) rather than a blue-white, the measure is capped at ~46rem, and body copy sits
at 1.65 line-height. One accent (deep teal) carries the primary action and the focus ring; every
other colour in the system means *state* — `warn` for substitution and degradation, `danger` for
failure. That is what keeps the provenance chip readable as information rather than decoration.

**Type.** Inter for interface and body, Source Serif 4 at display sizes only (login heading, empty
states), system mono for identifiers — model names, slots, token counts, request ids. A wire
identifier should look like one. Both families are self-hosted by `next/font`; no runtime font CDN.

**Message layout is asymmetric, not mirrored bubbles.** User turns are compact tinted blocks; model
answers are full-width prose with the indicator beneath. A 600-word answer in a right-aligned bubble
is unreadable, and most chat UIs inherit that shape without asking.

**Tokens, not `dark:` utilities.** `app/globals.css` defines the palette twice — `:root` and `.dark`
— and `@theme inline` maps semantic names onto it. Components say `bg-raised` and mean the right
thing in both themes. The theme (light / dark / system) is applied by a blocking inline script in
`app/layout.tsx` before first paint, so there is no flash; the choice itself lives in the account
modal as a segmented control.

> **Tailwind v4 trap, worth knowing before adding a token.** v3's arbitrary-variable form
> `rounded-[--radius-control]` compiles to `border-radius: --radius-control` — invalid CSS that
> fails *silently*, so every corner and shadow in the app quietly disappears. In v4, tokens declared
> under `@theme` generate real utilities: use `rounded-control`, `rounded-card`, `shadow-card`. And
> a theme token must never map onto a raw variable of the same name (`--shadow-card:
> var(--shadow-card)`), which resolves to nothing — hence the raw value is `--card-shadow`.

**Account and appearance live in a modal**, not in the sidebar chrome. Identity, plan and sign-out
sit behind one full-width button at the bottom of the sidebar, which opens a centred dialog over a
blurred backdrop. This is not a settings *page*: BYOK, model preferences and usage belong to later
phases, and the modal holds only what Phase 1 has.

**Audited contrast pairs** (WCAG AA, both themes):

| Pair | Light | Dark |
|---|---|---|
| `ink` on `ground` | 15.2:1 | 14.1:1 |
| `ink-secondary` on `ground` | 7.5:1 | 8.6:1 |
| `ink-tertiary` on `ground` | 4.7:1 | 5.3:1 |
| `accent` on `ground` | 6.2:1 | 7.4:1 |
| `accent-ink` on `accent` | 6.4:1 | 6.6:1 |
| `danger` on `ground` | 5.7:1 | 6.9:1 |
| `warn` on `ground` | 5.7:1 | 6.1:1 |

**Accessibility.** Skip link to the composer; `:focus-visible` ring globally and `outline: none`
nowhere; the sidebar is a `<nav>` with `aria-current` on the active row; the transcript is a
`role="log" aria-live="polite"` region so a new answer is announced; the composer is labelled, with
Enter to send and Shift+Enter for a newline (IME-safe); the delete confirm is a native `<dialog>`,
which brings a correct focus trap, Escape handling and focus restoration; the mobile drawer is
`inert` when closed and returns focus to its trigger on Escape.

**Responsive.** Persistent sidebar at `lg` and up, off-canvas drawer below. `100dvh` throughout and
`env(safe-area-inset-bottom)` on the composer, so the mobile keyboard doesn't push the message box
off screen.
