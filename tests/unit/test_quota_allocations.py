"""``app/quota/allocations.py`` — D39's personal cap, the grant-building half.

Pure functions, so these are unit tests with no Redis in sight — the atomic
check-and-increment machinery this grant plugs into is already covered by
``tests/unit/test_router.py``'s own D39 case and exercised end to end in
``tests/integration/test_chat_endpoint.py``.
"""

from __future__ import annotations

from uuid import UUID

from app.quota import allocations
from app.quota.tracker import WindowGrant
from tests.provider_fixtures import gemini_spec, groq_spec

USER_ID = UUID("11111111-2222-3333-4444-555555555555")


def test_no_cap_builds_no_grant() -> None:
    """D39: absent row, absent tier default — no cap, no reservation, no
    round trip beyond the ones the model's own windows already cost."""
    assert allocations.shared_pool_grants(gemini_spec(), user_id=USER_ID, cap=None) == ()


def test_a_cap_builds_exactly_one_request_counted_grant() -> None:
    grants = allocations.shared_pool_grants(gemini_spec(), user_id=USER_ID, cap=50)

    assert grants == (
        WindowGrant(
            window="rpd",
            limit=50,
            reset="rolling_daily",
            cost_is_tokens=False,
            key=f"q:{USER_ID}:gemini:gemini-3.6-flash:alloc:rpd",
        ),
    )


def test_the_grant_is_scoped_to_the_exact_provider_and_model() -> None:
    """A cap on one model must not silently fence a sibling model under the
    same provider — the key names both."""
    groq_grant = allocations.shared_pool_grants(groq_spec(), user_id=USER_ID, cap=10)

    assert groq_grant[0].key == f"q:{USER_ID}:groq:openai/gpt-oss-120b:alloc:rpd"
    assert groq_grant[0].window == "rpd"
    assert groq_grant[0].cost_is_tokens is False
