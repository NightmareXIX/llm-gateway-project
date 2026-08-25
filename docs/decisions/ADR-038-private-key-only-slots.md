# ADR-038 — Private-key-only slots are declared in config, and `auto` never picks one

**Status:** accepted · Phase 6, Step 9 · 2026-08-26
**Implements:** `phase6.md` §3 D41 (`project-overview.md` §9.7)
**Relates to:** [ADR-024](ADR-024-models-endpoint-shape.md) (what `/v1/models` promises),
[ADR-011](ADR-011-named-slot-spill.md) (D10's spill rule, unchanged by this),
[ADR-023](ADR-023-exact-cache-identity-and-scope.md) (why a per-user `auto` would poison a shared
cache), [ADR-014](ADR-014-latency-ranked-auto.md) (what `auto` is)

## Context

§9.7 says a private key "can unlock extra model slots… this falls out of the design for free — the
model registry just also checks the user's own `provider_keys` capabilities". It does not fall out
for free, because nothing in `providers.yaml` can express *"this slot needs a key the shared pool
does not have"*. The shared pool carries free-tier Gemini Flash; a paid Gemini Pro model is
unreachable with it, and there is no field that says so.

## Decision

**`Slot` gains `requires_private_key: bool = False`**, joining `internal` as a second visibility flag
— but with different semantics: `internal` slots are hidden from every client and unroutable by name;
these are client-facing and *conditionally* visible.

- `ProviderRegistry.requires_private_key(slot)` is backed by a `private_key_only_slots` frozenset
  computed in `build_registry`, exactly the way `internal_slots` already is.
- `list_models` includes such a slot only when the caller resolves `pool == "private"` for **every**
  provider in its candidate chain — `keys_resolution/resolver.py::resolves_private_for_every_provider`.
- `chat.py::_validate_slot` refuses it for anyone else with the **existing unknown-slot 400**, the
  same treatment `internal` already gets.
- **`auto` never includes a private-key-only slot's candidates**, for anybody, key holder included.
  `selection._fleet` — shared by `/v1/models`'s `auto` entry and by `route`/`route_stream` — skips
  them.
- `config/providers.yaml` gains a real `pro` slot (one Gemini Pro candidate), commented the way
  `perception` is: what it is for, and why the shared key genuinely cannot serve it.

The rejected alternative: **derive it from `KeyValidation.models`**. The validation call already
returns the model ids a key can reach, so a slot could appear when the user's key lists its model.

## Why

**Config declares it, because `KeyValidation.models` is a snapshot and this is a gate.** That list is
captured at add time and goes stale silently — a key whose entitlements changed upstream would either
hide a working slot or advertise a broken one, and the user has no way to tell which. `models` stays
populated and unpersisted (Contract A is untouched); the *gate* is a fact about the deployment, and
facts about the deployment live in `providers.yaml` with everything else. It is also the honest
answer to §9.7's "for free": the registry checking a user's capabilities is real, and it is one
boolean plus one resolver walk, not zero.

**Written for the general case even though the real one has a single provider.** A two-provider
private slot is a config edit away, and a check that assumes one provider is a trap that fires
silently — it would advertise a slot whose second candidate the user cannot reach, which is worse
than not advertising it at all.

**Refused with the *same* 400, not a new refusal shape.** A slot you cannot be served is not a slot
you should be told about, and a distinct "you need a key for this" error would leak the existence of
paid capability to every account and invite exactly the support conversation the hidden slot avoids.
`internal` already established this treatment; adding a second refusal shape would mean two places to
keep in sync for one idea.

**`auto` never selects one, and this is the load-bearing half.** `auto`'s promise is *the gateway
picks, on latency and availability* (ADR-014). Silently routing one user to a model only they can
reach makes their `auto` unreproducible — two accounts asking the same question get different
answers, with no disclosure that would explain why — and it makes their cache entries unshareable in
a cache that is keyed on slot, history and params and deliberately **not** on user (ADR-023). The
asymmetry that leaves is intentional: **asking for `pro` by name** still leads with its own candidate
and still spills into the rest of the (now private-slot-free) fleet on failover, so D10's spill rule
is unchanged. Explicit is explicit; `auto` stays a shared, reproducible decision.

## Consequences

- `/v1/models` genuinely differs between two accounts — both in the slot *list* and in each
  candidate's *status*, since Step 9 also fixed the endpoint to compute status under the caller's own
  scope. Before that fix it reported `rate_limited` for a budget a private-key holder was not
  spending, which was simply wrong the moment scopes diverged.
- The `pro` slot is the one Gemini candidate in `providers.yaml` that reserves **no** perception
  share. `test_gemini_candidates_reserve_half_their_budget_for_perception` had asserted every Gemini
  candidate anywhere reserved 0.5, which was only ever true because every prior one happened also to
  be a `perception` candidate; it now checks `reserved_fraction` against the models `perception`
  actually declares. D8's real invariant, not the one that happened to hold.
- `requires_private_key` is a *second* visibility flag with different semantics from `internal`, and
  the two are easy to conflate. `registry.slots()` still hides `internal` and still lists this one;
  the difference is documented on both fields.
- Adding or removing a key must refresh the client's model list, which is why `useProviderKeys`
  revalidates `/v1/models` on every successful write. A stale picker after an add is the one place a
  user would conclude the feature does not work.
- `KeyValidation.models` remains populated and unread — a seam for a later phase that wants to
  *explain* a slot rather than gate it ("your key does not list this model"), which is a genuinely
  different use and the one that list is actually good for.
