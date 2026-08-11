"""The §2.2.7 summarization seam — designed now, built later.

D4 says v1 fits a history by truncating it. Truncation is testable and costs
nothing; summarization costs a provider call and adds a failure mode, so it is
deliberately not in v1. What *is* in v1 is the shape it will take, because
retrofitting it later should be filling in this body rather than rewriting
:mod:`app.memory.fitting`.

Three design constraints are recorded here so they survive the gap:

**A summary replaces the messages it covers.** It never coexists with them in a
rendered payload. That is the invariant the ``covers_seq`` range exists to
enforce — the fitting step drops exactly ``[start, end]`` and inserts the summary
in their place, the same way it inserts an omission marker today.

**It is never on the request path.** Summaries are generated asynchronously after
a turn completes, cached on the conversation, and read from cache by the next
render. A user's message must never wait on a second provider call, which is also
why the cheapest available model does this work rather than the answering one.

**The block type stays reserved until this lands.** ``canonical.parse_content``
rejects ``{"type": "summary"}`` today, on purpose. This function returns the text
rather than a block so there is exactly one definition of that block's shape when
it arrives, in :mod:`app.memory.canonical` where the other five live.
"""

from __future__ import annotations

from app.memory.canonical import CanonicalMessage
from app.providers.base import ProviderAdapter
from app.providers.types import ModelSpec


async def summarize_range(
    messages: list[CanonicalMessage],
    *,
    covers_seq: tuple[int, int],
    adapter: ProviderAdapter,
    spec: ModelSpec,
) -> str:
    """Compress ``messages`` into the text of one ``summary`` block.

    ``covers_seq`` is the inclusive ``(start, end)`` range of ``seq`` values the
    result stands in for — the cache key on ``conversation_summaries``, and the
    range the fitting step removes when it uses the summary.

    A typed signature with no body, per the standing rule: a stub that quietly
    returned ``""`` would let a caller ship a payload whose history silently
    vanished.
    """
    raise NotImplementedError(
        "summarization-based context compression is designed in §2.2.7 and built after v1; "
        "the v1 fitting strategy is TRUNCATE (D4)"
    )
