"""Contract A — the ``ProviderAdapter`` protocol (§2.1.3) and shared HTTP plumbing.

This module is written *before* any adapter, deliberately. Implementing Groq
first and abstracting it afterwards produces an interface shaped like Groq, and
the whole value of the abstraction shows up in Phase 2 when Gemini — which puts
the model in the URL, the key in a header, the system prompt in a top-level field
and calls the assistant role ``model`` — has to fit the same protocol without
bending it.

**Two layers, on purpose.** :class:`ProviderAdapter` is a ``Protocol``: it states
what the router may assume and nothing else, and adapters satisfy it structurally
rather than by inheritance. :class:`HttpProviderAdapter` is an ordinary base
class holding the machinery every HTTP-speaking adapter would otherwise
duplicate. An adapter that is not HTTP-shaped — a local model, a test double —
implements the protocol without touching the base class, which is exactly the
freedom a ``Protocol`` buys over an ABC.

**On ``stream``'s signature.** §2.1.3 writes it ``async def stream(...) ->
AsyncIterator[StreamChunk]``. Read literally as a type that is a coroutine
*returning* an iterator, which is not what the contract's own prose describes
("must yield chunks as they arrive — no internal buffering"). An async generator
function's static type is ``def (...) -> AsyncIterator[StreamChunk]``, so that is
what the protocol declares. The semantics are the contract's; only the keyword
differs, and the payoff is that Phase 2 can drop in a real ``async def`` generator
with no change here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx

from app.core.logging import get_logger
from app.memory.canonical import CanonicalMessage
from app.providers.errors import ProviderError, Unavailable
from app.providers.types import (
    Completion,
    GenParams,
    KeyValidation,
    ModelSpec,
    QuotaHint,
    ResolvedAttachment,
    StreamChunk,
    Usage,
)

logger = get_logger("app.providers")

DEFAULT_CONNECT_TIMEOUT_S = 5.0
"""A provider that will not accept a connection in five seconds is down."""

DEFAULT_READ_TIMEOUT_S = 60.0
"""A completion legitimately takes tens of seconds.

Note how far this is from the shared client's ``read=10s``
(``app/main.py``): that value is sized for the JWKS fetch on the auth path.
Adapters share the client's connection pool but never its read timeout — every
call here passes its own."""

DEFAULT_IDLE_TIMEOUT_S = 30.0
"""Phase 2 (D1): the gap between stream deltas that counts as a stall.

The "slow but not down" case. A provider that accepts the connection and then
goes quiet never trips a total-request timeout, so without this it holds a
request open indefinitely and looks like latency rather than failure."""

DEFAULT_WRITE_TIMEOUT_S = 10.0
DEFAULT_POOL_TIMEOUT_S = 5.0


@runtime_checkable
class ProviderAdapter(Protocol):
    """Everything the router is allowed to assume about a provider.

    Nothing above this line in the call stack knows a provider's URL shape, error
    body format, or role vocabulary. If the router ever needs to branch on
    ``adapter.name``, something that belongs behind this interface has leaked
    through it.
    """

    name: ClassVar[str]

    # ---- payload construction --------------------------------------------- #
    def build_payload(
        self,
        messages: list[CanonicalMessage],
        spec: ModelSpec,
        params: GenParams,
        attachments: list[ResolvedAttachment],
    ) -> dict[str, Any]:
        """Canonical history in, provider request body out.

        **Pure.** No I/O, no clock, no randomness, no environment — which is what
        makes it golden-file testable and is the property §2.1.5's conformance
        suite checks by calling it twice and diffing.

        Handles system-prompt placement, role renaming, content-block shaping and
        native file embedding. Everything that differs between providers and is
        not a network concern happens in here.
        """
        ...

    # ---- execution --------------------------------------------------------- #
    async def complete(self, payload: dict[str, Any], key: str, timeout: float) -> Completion:
        """Execute a non-streaming generation.

        Raises a normalized :class:`~app.providers.errors.ProviderError` for every
        failure, including a 200 that carries nothing usable.
        """
        ...

    def stream(
        self,
        payload: dict[str, Any],
        key: str,
        timeout: float,
        idle_timeout: float,
    ) -> AsyncIterator[StreamChunk]:
        """Execute a streaming generation, yielding chunks as they arrive.

        No internal buffering — a chunk held back to be batched with the next one
        defeats the only reason streaming exists. Must enforce ``idle_timeout``
        between chunks and must raise a normalized error for mid-stream faults,
        which is the hard part: the HTTP status was already 200 by then.
        """
        ...

    # ---- normalization ----------------------------------------------------- #
    def parse_error(
        self, exc: Exception | httpx.Response, *, model: str | None = None
    ) -> ProviderError:
        """Turn anything that went wrong into one of the seven normalized classes.

        The load-bearing method of the entire contract. Must never raise: it is
        called on the failure path, and an exception here replaces a clean 502
        with a traceback.

        ``model`` is an addition to the contract's signature, defaulted so the
        one-argument form §2.1.3 specifies still works. A provider-level adapter
        instance serves every model that provider offers, so the failing model is
        genuinely not derivable from a ``ConnectError`` alone — and an error that
        cannot name its model cannot open the right circuit breaker.
        """
        ...

    def extract_usage(self, response_body: dict[str, Any]) -> Usage:
        """Read token counts from a response, or estimate them.

        Falls back to :meth:`estimate_tokens` with ``estimated=True`` rather than
        returning zeros — a zero is indistinguishable from a real measurement and
        would silently under-count a daily budget.
        """
        ...

    def estimate_tokens(self, payload: dict[str, Any]) -> int:
        """Pre-call estimate, for quota reservation.

        A cheap approximation is fine and correct: the reservation is reconciled
        against actual usage at commit time, so this only has to be close enough
        that a burst of concurrent requests cannot overshoot a limit.
        """
        ...

    # ---- operations -------------------------------------------------------- #
    async def validate_key(self, key: str) -> KeyValidation:
        """The cheapest liveness check this provider offers.

        Used by the BYOK add-key flow (§9.2) and a startup health check. Must
        raise rather than return ``valid=False`` when the provider is
        unreachable — "we could not check" is not "your key is bad".
        """
        ...

    def rate_limit_headers(self, response: httpx.Response) -> QuotaHint | None:
        """Ground truth from the provider's own rate-limit headers, when it sends any.

        Opportunistic. Phase 3 uses it to correct drift in locally-counted
        budgets; a provider that publishes nothing yields ``None`` and costs the
        tracker nothing.
        """
        ...


class HttpProviderAdapter:
    """Shared machinery for adapters that talk HTTP.

    Holds no per-request state, so one instance serves the whole process and
    every model that provider offers. The ``httpx.AsyncClient`` is *injected*
    rather than constructed here: the process keeps exactly one connection pool
    (``app.state.http_client``), and a client built per adapter would multiply
    TLS handshakes for no benefit and leak in tests.
    """

    name: ClassVar[str]

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        options: Mapping[str, str] | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._options = dict(options or {})

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def options(self) -> Mapping[str, str]:
        """Non-secret per-deployment settings from ``providers.yaml``.

        Empty for every provider that declares none, which is why the parameter
        is defaulted: an adapter with nothing to configure should not have to
        know this exists. OpenRouter's attribution headers are the first user.
        """
        return self._options

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _timeout(self, read_s: float) -> httpx.Timeout:
        """A per-request timeout override.

        Only ``read`` varies by call — connect, write and pool are properties of
        the network, not of how long a model takes to think.
        """
        return httpx.Timeout(
            connect=DEFAULT_CONNECT_TIMEOUT_S,
            read=read_s,
            write=DEFAULT_WRITE_TIMEOUT_S,
            pool=DEFAULT_POOL_TIMEOUT_S,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        model: str | None = None,
    ) -> httpx.Response:
        """Every outbound call goes through here.

        Returns the response whatever its status — deciding what a 429 means is
        ``parse_error``'s job, and raising here would put that decision in two
        places. What this *does* own is the transport layer: a refused connection,
        a DNS failure, a read timeout, a truncated response. Those never produce
        an ``httpx.Response`` at all, so nothing downstream could normalize them.
        """
        try:
            return await self._client.request(
                method,
                self._url(path),
                headers=headers,
                json=json,
                timeout=self._timeout(timeout_s),
            )
        except httpx.HTTPError as exc:
            error = self.parse_error(exc, model=model)
            logger.warning("provider.transport_error", **error.log_fields(), path=path)
            raise error from exc

    # ---- defaults subclasses may keep ------------------------------------- #
    def parse_error(
        self, exc: Exception | httpx.Response, *, model: str | None = None
    ) -> ProviderError:
        """Conservative fallback. Real adapters override this — it is the contract's
        whole point — but the base must still answer, because :meth:`_request`
        calls it and a half-built adapter should fail as ``Unavailable`` rather
        than ``AttributeError``."""
        return Unavailable(
            str(exc),
            provider=self.name,
            model=model or "unknown",
        )

    def rate_limit_headers(self, response: httpx.Response) -> QuotaHint | None:
        """Providers that publish nothing need not override this."""
        return None

    def stream(
        self,
        payload: dict[str, Any],
        key: str,
        timeout: float,
        idle_timeout: float,
    ) -> AsyncIterator[StreamChunk]:
        """Phase 2 seam.

        A plain ``def`` rather than an ``async def`` with a ``yield``, so calling
        it raises immediately. An async generator would return a perfectly
        well-formed object and only fail on the first ``__anext__`` — deep inside
        an SSE response, after the headers were already sent.
        """
        raise NotImplementedError(
            f"{self.name}: streaming lands in Phase 2 with the D1 restart state machine"
        )
