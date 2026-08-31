"""D46: simulated cost, computed at read time from ``config/pricing.yaml``.

One pure function. Nothing here does I/O — :func:`app.config.get_pricing_config`
already caches the parsed table, so a lookup here is a dict access two levels
deep.

``Decimal``, not ``float``: this number gets summed across thousands of requests
by Phase 7 Step 2's aggregates and rendered as currency, and a ``float`` total
drifts from the number a spreadsheet would compute over the same rows.

``None`` for an unpriced model, never ``Decimal("0")`` (trap 7). An unpriced
model is a gap in the price table, not a model that costs nothing — conflating
the two makes a dashboard's total quietly understate itself in the flattering
direction the moment someone adds a candidate to ``providers.yaml`` without
adding it to ``pricing.yaml``.

:func:`default_currency` is Phase 7 Step 3's one addition: ``/v1/admin/usage``
reports a currency alongside its total, and this module is the only one
:func:`app.config.get_pricing_config` may be read from directly (Step 1's own
"done when" — nothing outside this module and ``config.py`` imports the
table). Every entry in the checked-in table declares ``usd`` today; this
assumes the whole table shares one currency rather than mixing them, which is
true of the committed file and would need revisiting the day it stops being
true.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import get_pricing_config

_ONE_MILLION = Decimal(1_000_000)


def simulated_cost(provider: str, model: str, *, tokens_in: int, tokens_out: int) -> Decimal | None:
    """What ``tokens_in``/``tokens_out`` on this (provider, model) would have
    cost at its published list price. ``None`` when the model has no pricing
    entry; ``Decimal("0")`` when it does and both token counts are zero — a
    priced model that used nothing cost nothing, a different fact from an
    unpriced one.
    """
    entry = get_pricing_config().for_model(provider, model)
    if entry is None:
        return None

    input_cost = Decimal(tokens_in) * entry.input_per_mtok
    output_cost = Decimal(tokens_out) * entry.output_per_mtok
    return (input_cost + output_cost) / _ONE_MILLION


def default_currency() -> str | None:
    """The price table's currency, assumed uniform across every entry.

    ``None`` for an empty table — equally fictional, and equally not worth a
    dollar sign. Scans rather than reading a top-level ``currency`` key because
    ``PricingConfig`` has none; adding one would duplicate a fact ``PricingEntry``
    already states once per row for no reader that needs it per-row today.
    """
    pricing = get_pricing_config()
    for entries in pricing.pricing.values():
        for entry in entries.values():
            return entry.currency
    return None
