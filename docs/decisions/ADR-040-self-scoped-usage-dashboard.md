# ADR-040 — The usage dashboard is self-scoped, and there is no admin identity

**Status:** accepted · Phase 7, Steps 2–3 (surface), Step 9 (page) · 2026-09-01
**Implements:** `phase7.md` §3 D44 and D45 (`development-plan.md` §3 Phase 7,
`project-overview.md` §4.8, §14)
**Relates to:** [ADR-007](ADR-007-auth-model.md) (the frozen `Principal` this does not widen),
[ADR-024](ADR-024-models-endpoint-shape.md) (the endpoint `/v1/admin/quota` delegates to),
[ADR-034](ADR-034-per-candidate-credential-resolution.md) (which pool "your quota" even means)

## Context

`development-plan.md` §3 asks Phase 7 for "a usage dashboard against `api/admin.py`" and
`project-overview.md` §14 calls it "a minimal admin dashboard". `app/api/admin.py` has been a named,
empty slot in §3's repo tree since Phase 1.

The word *admin* is the problem. `Principal` is frozen at four fields (`user_id`, `auth_method`,
`api_key_id`, `tier`) and the `users` table has no role column. A genuine admin surface needs an
authorization axis this system does not have, and there were exactly three ways to get one, all bad
in the last week of the project:

- add `is_admin` to `users` and a fifth field to `Principal` — widening a frozen contract for a
  read-only chart;
- an environment allowlist of admin emails — a second authorization mechanism beside D7's, with its
  own failure modes, tested by nobody;
- a separate admin token — a third credential type in a system that already argued carefully for
  having exactly two.

Each buys the same thing: the ability to see *other people's* numbers, on a portfolio project whose
deployed instance has one real user and a demo account.

## Decision

**Every route in `api/admin.py` is scoped to the calling principal's own `user_id`, in the SQL
itself, exactly like `conversations` and `files`.** No role, no allowlist, no `is_admin`, no
`Principal` change. The module keeps its designated name because §3's tree named it and renaming a
slot is churn; its docstring says in its first paragraph that "admin" here means *this account's own
operational view*, not everyone's.

Three routes, and the two panels that are not per-user data are handled explicitly rather than
fudged:

- `GET /v1/admin/usage` — Step 2's four aggregates (`volume_series`, `provider_distribution`,
  `outcome_summary`, `pool_split`), each of which takes `user_id` as a required keyword and puts it
  in its own `WHERE` clause.
- `GET /v1/admin/quota` — **quota utilization is a property of a pool, not of a user**, and which
  pool the caller draws from is exactly what Phase 6's resolver answers (ADR-034). So this route
  takes no `PrincipalDep` of its own and re-derives nothing: it calls `api/v1/models.py::list_models`
  directly with this request's own registry, breaker, tracker and `CredentialsDep`. A shared-pool
  user sees the shared pool's remainder — which `/v1/models` already shows them — and a private-key
  user sees their own counters. No new disclosure, and the two pages cannot quietly disagree because
  there is only one computation.
- `GET /v1/admin/requests` — the pre-existing `list_for_user`, mapped to a wire model. The "my last
  few calls" table under the charts.

**Breaker state is not duplicated into this module.** It is global, and it is already visible
per-candidate through `/v1/models` (and therefore through `/quota` above). A second copy would be a
second thing to keep true.

## Why

**A truthful product feature beats a fake ops console.** "Here is *your* usage — what you spent,
which providers answered you, how often the cache saved a call" is a real feature a real product
ships. A page that claims to be an operations console on a system with no operators is a demo of a
thing that does not exist, and the first question an interviewer asks about it ("who is allowed to
see this?") has no good answer.

**The aggregates take `user_id`, and that is the seam.** If this project ever grows a real operator
identity, the change is `user_id: UUID` → `user_id: UUID | None` on the four functions in
`db/repo/requests.py`, with `None` meaning "every row", plus whatever gates the routes. That is the
whole change — the SQL is already grouped and filtered in the right place, nothing loads rows into
Python, and no caller outside `api/admin.py` reads them. Recording it here is cheaper than
pre-building it.

**Self-scoping is a security property, not just a scoping convention.** `pool_split` is the sharpest
case: `requests.quota_scope` literally carries a user's own id on a private-key turn, so a leak
there is a leak of *whose* private key paid for what. The integration suite seeds a second user's
private rows alongside the caller's on every one of the four aggregates and asserts they never
appear.

**D51 (the dashboard is a page in the existing Next.js app, with hand-rolled SVG charts) gets no ADR
of its own**, following the precedent ADR-032 set for D33. Once this decision fixed the auth story,
"a `/usage` route behind the same Supabase session, using the existing design system and `lib/api.ts`"
had no live alternative worth recording — the rejected option was a second app (Streamlit) meaning a
second deploy, a second auth story and a second visual language, for a page that is four charts over
one JSON document. The no-chart-library half is the same argument ADR-013 already makes about
`pybreaker` and ADR-044 makes about `prometheus_client`, and the charts' own docstrings carry it.

## Consequences

- `user_quota_allocations` gets **no write surface** this phase. D39 built the table and the read
  (`allocations_repo.get_cap`, `list_for_user`); rows are still written by hand, because there is no
  operator to use an editor and building one would re-open exactly the question this ADR closed.
  `allocations_repo.set_cap` stays an unbuilt, named seam.
- The dashboard cannot answer "is the *system* healthy" — only "what did *my* traffic do". The
  system-wide view is `/metrics` (ADR-044), which is scraped rather than browsed and is protected by
  a bearer token rather than by a session.
- The three routes return 401 unauthenticated like every other authenticated route; there is no 403
  anywhere in the module, because there is no role to fail.
- Two accounts hitting `/v1/admin/usage` in the same test get two different, correct answers. That
  test is the whole enforcement mechanism, and it is deliberately written against the HTTP surface
  rather than the repo functions.
