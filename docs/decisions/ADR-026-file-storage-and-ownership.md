# ADR-026 — Private, content-addressed storage; global bytes, per-user ownership

**Status:** accepted · Phase 4, Steps 1–4 · 2026-08-22
**Implements:** `phase4.md` §3 D23, D24
**Relates to:** [ADR-017](ADR-017-render-as-deploy-target.md) (Render's ephemeral filesystem, the
reason bytes cannot live in the container); the hard rule "every conversation read is
ownership-scoped in the SQL query itself" (`CLAUDE.md`), which D24 extends to files

## Context

Two separate questions, both new to Phase 4 and both wrong to answer with the same mechanism. First:
where do uploaded bytes actually live, given that Render's free plan offers an ephemeral filesystem
and a service that sleeps — anything written to local disk is gone on the next deploy or the next
cold start. Second: a `file_hash` is 64 hex characters and therefore unguessable, but "unguessable" is
not an authorization model, and the gateway's standing rule is that every read is scoped in the query
itself, never fetched and then checked.

## Decision

**Storage: a Protocol, three implementations, one setting.** `ObjectStore` (`put`/`get`/`exists`) is
backed by `SupabaseStore` in every deployed environment — Supabase Storage over the app's *existing*
shared `httpx.AsyncClient`, the same pool and timeouts every provider adapter already uses, no second
SDK and no second connection. `LocalStore` serves a dev box without Storage configured; `MemoryStore`
is what the test suite uses, so a byte never touches a disk or a network in CI.
`FILES_STORAGE_BACKEND` picks one at startup.

**The bucket is private and stays private.** No public URL is ever generated, no signed URL is ever
handed to a client, and there is no download endpoint anywhere in this phase. The only reader of the
bytes is the gateway itself, resolving an attachment for a model.

**Path is content-addressed:** `{hash[:2]}/{hash}` (`object_path`). Dedup falls out of this for
free — two uploads of identical bytes write the same object once.

**Ownership: the bytes and the extraction are global; the right to reference them is per user.**
`files` is unique on `(user_id, file_hash)` — two users uploading identical bytes get two rows and
one object. `file_extractions` is keyed on `file_hash` alone, with no `user_id` column at all,
because the extracted text of a byte sequence is a property of those bytes, not of who uploaded them.

**A `file_ref` is validated once, at the point it enters a message.** `POST /v1/chat/completions`
resolves every hash across every message in the request with one query —
`WHERE file_hash = ANY(:hashes) AND user_id = :uid` — before a single message row is written. A hash
the caller does not own is missing from the result, and a missing hash is a **404**, never a 403 —
the same rule `GET /v1/conversations/{id}` already follows, because a 403 would confirm the hash names
real bytes and a 404 does not.

## Why

**`SupabaseStore` reuses the app's existing HTTP client on purpose.** A second SDK or a second
connection pool for one more Supabase product would be new failure surface for no capability the
shared client does not already provide — the same reasoning that keeps every provider adapter on one
`httpx.AsyncClient`.

**A private bucket with no download path removes a whole category of bug before it can be written.**
"A file hash is a capability" is exactly the shape of vulnerability a signed URL or a public bucket
would introduce — anyone holding the hash could fetch the bytes regardless of the `files` row's
`user_id`. Making the gateway the only possible reader means the ownership check in Contract-adjacent
code is the *only* check that matters, rather than one of two that have to agree.

**Content-addressed paths make dedup a property of the storage layer, not application logic.** Two
uploads of the same PDF write to the same path; the second `put` either succeeds trivially or — on
Supabase specifically — arrives as a 409 that `SupabaseStore._logical_status` reads as success,
because a conflict on a content-addressed path can only mean identical bytes are already there.

**Splitting ownership from content is what makes invariant 6 affordable.** If `file_extractions` were
scoped by user, two users uploading the same report would pay for two extractions of the same bytes —
spending the perception lane's fenced-off, deliberately scarce budget (D26) to compute one string
twice. Keying it on the hash alone means the fleet extracts a given document exactly once, no matter
how many users hold it.

**404, never 403, is the same reasoning applied a second time.** A 403 on somebody else's hash would
leak one bit of information — "this hash names real bytes, you just can't have them" — that a 404
does not. The gateway already made this call for conversations; files inherit it rather than
reopening the question.

## Consequences

- There is no file management UI, no browser, no delete endpoint, and no re-download path in this
  phase — a direct consequence of "the gateway is the only reader," not a scope cut made
  independently of it. `GET /v1/files/{hash}` returns metadata only.
- `SUPABASE_SERVICE_ROLE_KEY` is a new secret with more authority than anything the gateway
  previously held — it bypasses row-level security on the whole Supabase project. It is used for
  exactly one thing, is never logged, and every `StorageUnavailable` raised by `storage.py`
  deliberately carries neither the key nor the bytes (trap 17).
- `db/repo/files.py` deliberately has **no** "does anybody own this hash" query. Whether the object
  exists is `ObjectStore.exists`'s question; whether *this* user may reference it is `files`'s. A repo
  function answering "who else has this" would be exactly the kind of cross-user probe D24 exists to
  prevent.
- Deploying this phase is not purely a code change: the Supabase Storage bucket has to be created and
  explicitly kept private before the first upload reaches production, documented as its own step in
  `docs/deploy.md` rather than assumed.
