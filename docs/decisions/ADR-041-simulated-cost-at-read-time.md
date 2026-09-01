# ADR-041 — Simulated cost is computed at read time, and an unpriced model is not free

**Status:** accepted · Phase 7, Step 1 (the table and the function), Step 3 (the aggregate) · 2026-09-01
**Implements:** `phase7.md` §3 D46 (`project-overview.md` §4.8)
**Relates to:** [ADR-040](ADR-040-self-scoped-usage-dashboard.md) (the only reader),
[ADR-019](ADR-019-quota-window-model.md) (the other place `limits.yaml`-shaped config is validated at
boot)

## Context

§4.8 asks for "simulated cost" on the usage dashboard: everything here runs on free tiers, so no
money changes hands, but *"what would this traffic have cost at list prices"* is the number that
makes a gateway's routing decisions legible. It is also the number most likely to be quietly wrong.

Two questions had to be settled before writing it. Where does the price live — a column on
`requests`, or a table consulted at read time? And what does a model with no price entry contribute?

## Decision

**`config/pricing.yaml`, loaded by a fourth `lru_cache`d `get_pricing_config()` in `app/config.py`
and validated by `validate_startup_config()` alongside the other three config sources.**
`PricingEntry`/`PricingConfig` mirror `ModelLimits`/`LimitsConfig` exactly — `extra="forbid"`,
`frozen=True`, a `for_model` lookup — because a fourth config shape that behaves differently from the
first three is a fourth thing to remember.

**`app/usage/pricing.py::simulated_cost(provider, model, *, tokens_in, tokens_out) -> Decimal | None`
is one pure function, and the only place the table is read.** Nothing outside that module and
`config.py` imports `get_pricing_config`, asserted by `grep` in the step's own "done when".

**No cost column is added to `requests`.** Prices change. A stored number freezes a fiction at the
moment it was written and then disagrees, silently and forever, with the table it came from. Computed
at read time, the number is always "what this traffic would cost at *today's* published rates", which
is the only claim the feature can honestly make.

**A model with no price entry contributes `None`, never `Decimal("0")`.** `/v1/admin/usage` returns
`total_cost` *and* `unpriced_requests`, and the page renders "≈ $0.42 across 1,203 requests
(18 unpriced)". A window in which nothing at all was priced reports `total_cost` and `currency` as
`null`, not zero.

**A missing entry is a warning at boot (`config.unpriced_models`), not a `ConfigError`.** Every other
config failure in this project is fatal; this is the one deliberate exception.

## Why

**`Decimal`, never `float`.** This number is summed across thousands of rows and rendered as
currency. A `float` total drifts from what a spreadsheet computes over the same rows, and the drift
shows up as a total that is wrong in the last two digits — which is worse than being obviously wrong,
because it looks right. The conversion to a JavaScript number happens once, in `formatCost`, at the
last possible moment before display; the wire type is a **string**, because that is how pydantic
serializes a `Decimal` and parsing it early would undo the whole point.

**`0` for unpriced is a lie in the flattering direction, which is the worst kind** (trap 7). The
moment somebody adds a candidate to `providers.yaml` without adding it to `pricing.yaml`, a silent
zero makes the dashboard under-report — and the failure is invisible, because a total that is too low
looks like good news. `None` propagates to a count the page prints beside the total, and the page
says out loud that an unpriced model is unpriced rather than free.

**Killing the process over a fictional price table would be disproportionate.** A gap in
`pricing.yaml` cannot make a served request wrong; it can only make one chart incomplete, and the
chart already discloses its own incompleteness. Refusing to serve real traffic over it inverts the
severity. The loader's docstring carries this sentence, because it is the exception to a rule the
rest of the codebase follows without one.

**The pool split gets a blended rate, and says so.** `pool_split` has no per-(provider, model)
breakdown to cost directly — it partitions on `quota_scope` — so each side's cost is the window's
total cost divided by the priced tokens behind it, applied to that side's own token counts. That is
exact only when every priced request in the window shares one per-token price, which is generally
false (input and output are priced differently). `PoolSplitOut`'s docstring and the page both call it
an approximation rather than a ledger. A side with zero tokens of its own still costs exactly
`Decimal("0")` and not `None` — it spent nothing, which is a different fact from being unpriced, and
the same distinction `simulated_cost` itself draws.

## Consequences

- The README says, in the dashboard's own section, that nothing was billed and the number is
  computed now from a checked-in table. That sentence is a better answer than the number.
- `config/pricing.yaml` prices every candidate the committed `providers.yaml` routes to, including
  `pro`'s `gemini-3.6-pro` and both OpenRouter `:free` models — a `:free` model has a real list price
  at its non-free tier, and pricing it at zero would make the dashboard claim OpenRouter is free
  rather than that this tier of it is.
- Changing a price changes every historical number on the dashboard. That is the intended
  consequence of read-time computation and is stated in `docs/limitations.md`; a system that needed
  a stable historical ledger would need a stored cost column and a price-version key, which is a
  different feature.
- Adding a provider means touching two config files. The boot warning is what makes the second one
  hard to forget, and `tests/unit/test_config.py` asserts every enabled candidate in the committed
  fleet is priced.
