"""Kill providers under load and watch the gateway not notice.

The demo `development-plan.md` §3 asks Phase 7 for, and the one artifact in this
repo that shows the whole machine working at once: N concurrent clients, a fleet
of providers that dies and comes back on a schedule, and a summary whose headline
number is the one that should be zero — **client-visible failures**.

**It drives the real application** (D50). ``create_app()`` over
``httpx.ASGITransport``, the real router, the real breaker, the real persistence.
The only thing faked is the far side of the wire: an ``httpx.MockTransport``
serving the same recorded fixtures the test suite commits, whose per-candidate
behaviour this script flips as the run progresses. **Nothing in ``app/`` changes
for this script, and nothing may.** If the demo ever needs a hook in production
code, the demo is wrong — a chaos toggle reachable from a deployed service is a
permanent hole punched in the gateway for the sake of one recording.

Run it::

    docker compose up -d postgres
    python -m scripts.chaos_demo                     # 90s, 4 clients, seed 1
    python -m scripts.chaos_demo --seed 7 --json run.json
    python -m scripts.chaos_demo --duration 30 --concurrency 2

`docs/chaos-demo.md` is the companion: what the numbers mean, a captured
transcript, and what an in-process mock does *not* demonstrate.

What the script arranges for itself, and why each one is a deliberate choice
rather than a convenience:

**Its own user.** A ``users`` row and a ``gw_live_`` API key are inserted
directly and the load is driven through the real API-key auth path — the
programmatic half of D7, and the half that does not need a browser. The rows are
deleted afterwards (``ON DELETE CASCADE`` takes the conversations, messages and
``requests`` rows with them) unless ``--keep-data`` says to leave them, which is
what you want if the next thing you look at is ``/usage``.

**Redis is ``fakeredis`` by default.** Breaker state and quota counters that
survived the previous run would make the first minute of this one a replay of the
last one's outages. ``--redis-url`` points at a real server for anyone who wants
the cross-process article; the fakeredis import is local to that branch, so the
script still runs where the dev extra is not installed.

**Four settings are overridden, all of them documented switches, none of them
new.** ``RATE_LIMIT_ENABLED=false``: D20's limiter caps one user at 20 requests a
minute, and this demo *is* one user by construction — leaving it on would measure
the limiter rather than failover. ``QUOTA_ENFORCEMENT=false``: the windows in
``config/limits.yaml`` describe real free-tier budgets measured in tens of
requests per minute, and a load demo saturates them in seconds, which drowns the
outage story in quota skips (Phase 3's own suite covers that behaviour, and
``--quota`` turns it back on for anyone who wants to watch it happen).
``ROUTING_LATENCY_RANKING=false``: D11 reorders candidates once it has samples,
and a reordering fleet makes two runs of one seed disagree about who served what.
``FILES_STORAGE_BACKEND=memory``: no request here carries a file, and the default
backend wants a Supabase service key to construct.

**Reproducibility.** ``--seed`` fixes the workload — which slot each request
names, which ones stream, in what order — and the schedule is expressed in
*rounds*, not wall-clock seconds, so the same seed sends the same requests into
the same fleet states every time. The summary is split accordingly: the
``plan`` block — the seed, the workload it produced, the phase schedule — is
byte-identical between two runs of one seed, and the ``outcome`` block is
measured. Of the measured half, the headline reproduces (every request answered,
nothing client-visible) while the per-candidate counts move by a request or two,
because a breaker's thirty-second cooldown expires against a wall clock rather
than a round counter. Claiming the whole summary was reproducible would be the
kind of dishonesty the rest of this project spends its disclosure fields
avoiding.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from redis.asyncio import Redis

if TYPE_CHECKING:
    from app.config import ProvidersConfig, Settings
    from app.routing.circuit_breaker import CircuitBreaker

# --------------------------------------------------------------------------- #
# Fixtures — read as files, not imported from `tests`
# --------------------------------------------------------------------------- #
# `scripts/record_fixtures.py` writes this directory and deliberately does not
# import the test package; this reads it back under the same rule. A script that
# imported `tests` would make running the demo depend on the suite's fixtures
# module, its conftest and its pytest plugins, none of which it needs.
REPO_ROOT: Final = Path(__file__).resolve().parent.parent
FIXTURE_ROOT: Final = REPO_ROOT / "tests" / "fixtures" / "provider_responses"

COMPLETIONS: Final = "/v1/chat/completions"

type Health = Literal["healthy", "rate_limited", "unavailable"]

HEALTHY: Final[Health] = "healthy"
RATE_LIMITED: Final[Health] = "rate_limited"
UNAVAILABLE: Final[Health] = "unavailable"

FIXTURE_FOR: Final[dict[Health, str]] = {
    HEALTHY: "success",
    RATE_LIMITED: "rate_limited",
    UNAVAILABLE: "server_error_html",
}
"""Which recorded response each health state serves.

``server_error_html`` rather than a JSON 5xx on purpose: it is a 502 whose body
is a load balancer's HTML error page, which is what a provider outage actually
looks like from the outside and the case ``parse_error`` most often gets wrong.
"""

STATE_GLYPH: Final[dict[Health, str]] = {
    HEALTHY: "ok",
    RATE_LIMITED: "429",
    UNAVAILABLE: "502",
}


# --------------------------------------------------------------------------- #
# The schedule
# --------------------------------------------------------------------------- #
SLOTS: Final[tuple[str, ...]] = ("auto", "general", "fast")
"""The slots the workload names. ``pro`` is excluded — D41 makes it invisible to
a caller without their own Gemini key, and this demo holds none."""

STREAM_FRACTION: Final = 0.25
"""Roughly one request in four is streamed, so the streaming path and the ``done``
event's own disclosure are exercised rather than assumed."""


@dataclass(frozen=True, slots=True)
class Phase:
    """One stretch of the run, and what is broken during it.

    ``kills`` is keyed by either a provider (``"groq"``) or a single candidate
    (``"groq/openai/gpt-oss-120b"``), and the more specific key wins. Both
    granularities are needed: an outage is per provider, but the one thing a
    per-provider kill can never produce in this fleet is a **substitution** —
    every slot is backed by the same three providers, so killing a provider is
    always absorbed by failover *inside* the slot. Emptying one slot's candidate
    list while another's survives takes a per-candidate kill, which is what the
    fourth phase below is for.
    """

    name: str
    rounds: int
    kills: dict[str, Health] = field(default_factory=dict)
    note: str = ""


MAX_ATTEMPTS: Final = 3
"""``routing/router.py``'s own cap (D1), restated because the schedule is built
around it. A skipped candidate is not an attempt; a failed one is."""

BREAKER_FAILURE_THRESHOLD: Final = 5
"""``routing/circuit_breaker.py``'s own threshold, for the same reason."""

BREAKER_COOLDOWN_S: Final = 30.0
"""``routing/circuit_breaker.py``'s first cooldown, ditto — and the constraint
that sizes the outage phases below."""

GENERAL_SHARE: Final = 2 / 3
"""How much of the workload reaches ``general``'s first candidate: the two slots
of three that lead with it (``general`` names it, ``auto`` flattens to it)."""

GENERAL_CANDIDATES: Final[tuple[str, ...]] = (
    "groq/openai/gpt-oss-120b",
    "gemini/gemini-3.6-flash",
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
)
"""``general``'s chain, in order. Named literally rather than read off the config
because the fourth phase is a statement *about* this slot — if the YAML changes,
the phase's own claim has to be re-derived, not silently retargeted."""


def build_schedule(rounds: int, *, concurrency: int, interval: float) -> tuple[Phase, ...]:
    """The phases, sized against D1's attempt cap and the breaker's cooldown.

    Not weights over the run length, because the interesting phase only works
    inside a window the *gateway's* own constants define:

    1. **Steady state.** A baseline the rest can be read against.
    2. **``general``'s first candidate rate-limited.** A 429 is not retryable on
       the same provider, so a request fails over on attempt two and nobody
       notices. Long enough to land five failures on that candidate, which opens
       its breaker — the thing phase 4 spends.
    3. **Groq and OpenRouter both down, every model.** Two of three providers.
       Gemini carries the fleet; ``fast``'s Groq candidate burns its one
       same-provider retry (an ``Unavailable`` *is* retryable, unlike a 429) and
       still lands on attempt three.
    4. **All three of ``general``'s candidates rate-limited, ``fast``'s healthy.**
       The only shape in this fleet that can produce a **substitution**: every
       slot is backed by the same three providers, so a per-provider outage is
       always absorbed by failover *inside* the slot. Here ``general``'s first
       candidate is skipped (phase 2 opened its breaker, and a skip is free), the
       other two spend one attempt each — 429s, so neither is retried — and the
       third attempt spills into ``fast``'s chain. A different slot answers a
       named request, and ``substituted`` says so (D2).
    5. **Recovery.** Everything healthy; the cooldowns opened in phases 2 and 3
       expire, their half-open probes succeed, and the breakers close.

    Two constraints size the middle three phases, and both come from code:

    - Phase 2 has to land ``BREAKER_FAILURE_THRESHOLD`` failures, and only the
      ``general``/``auto`` share of the workload reaches that candidate.
    - Phases 2 to 4 together have to finish **inside one cooldown**, or the probe
      that fires mid-phase-4 spends the attempt the spill needs.

    When the run is too short to honour both — a small ``--duration`` or a single
    client — phase 4 is **dropped** rather than run into a failure it would
    deserve. A short run demonstrates failover; the cascade needs the defaults.
    """
    warm = max(1, rounds // 10)
    trip = max(2, ceil((BREAKER_FAILURE_THRESHOLD + 1) / max(1.0, concurrency * GENERAL_SHARE)))
    cooldown_budget = max(1, int((BREAKER_COOLDOWN_S - 4.0) / interval) // 3)
    outage = min(max(trip, rounds // 6), cooldown_budget)

    steady = Phase("steady state", warm, {}, "every candidate healthy")
    rate_limited = Phase(
        "groq/gpt-oss-120b rate-limited",
        outage,
        {GENERAL_CANDIDATES[0]: RATE_LIMITED},
        "429 on general's first candidate",
    )
    two_down = Phase(
        "groq + openrouter down",
        outage,
        {"groq": UNAVAILABLE, "openrouter": UNAVAILABLE},
        "two of three providers gone",
    )
    spill = Phase(
        "general's whole chain rate-limited",
        outage,
        dict.fromkeys(GENERAL_CANDIDATES, RATE_LIMITED),
        "general spills into fast (a substitution)",
    )

    cascade_fits = outage >= trip and rounds >= warm + 3 * outage + warm
    body = [steady, rate_limited, two_down, spill] if cascade_fits else [steady, rate_limited]
    recovery = max(1, rounds - sum(phase.rounds for phase in body))
    body.append(Phase("recovery", recovery, {}, "breakers re-probe and close"))
    return tuple(body)


def routable_candidates(config: ProvidersConfig) -> list[tuple[str, str]]:
    """Every ``(provider, model)`` a request from this demo can actually reach.

    Read off the slot table rather than off the registry, because the transport
    has to know the fleet before the registry that routes through it exists.
    ``internal`` slots are skipped because no client can name one, and
    ``requires_private_key`` slots because this demo holds no private key (D41) —
    both of their candidates would otherwise show up in the live table as
    permanently unserved rows.
    """
    candidates: list[tuple[str, str]] = []
    for slot in config.slots.values():
        if slot.internal or slot.requires_private_key:
            continue
        for candidate in slot.candidates:
            if (candidate.provider, candidate.model) not in candidates:
                candidates.append((candidate.provider, candidate.model))
    return candidates


def phase_for_round(schedule: Sequence[Phase], index: int) -> Phase:
    """Which phase round ``index`` falls in. Pure, so the transport is too."""
    cursor = 0
    for phase in schedule:
        cursor += phase.rounds
        if index < cursor:
            return phase
    return schedule[-1]


# --------------------------------------------------------------------------- #
# The scripted upstream
# --------------------------------------------------------------------------- #
def load_fixture(provider: str, name: str) -> httpx.Response:
    """One recorded response, rebuilt as an ``httpx.Response``."""
    raw: dict[str, Any] = json.loads(
        (FIXTURE_ROOT / provider / f"{name}.json").read_text(encoding="utf-8")
    )
    response: dict[str, Any] = raw["response"]
    headers: dict[str, str] = response.get("headers", {})
    text: str | None = response.get("text")
    if text is not None:
        return httpx.Response(response["status"], headers=headers, text=text)
    return httpx.Response(response["status"], headers=headers, json=response.get("body"))


def load_stream(provider: str) -> httpx.Response:
    """The recorded streaming success, as a whole body.

    Not trickled out chunk by chunk: the demo measures the gateway's behaviour
    under failure, not its idle-timeout handling, and a real delay per chunk
    would make every latency in the summary a measurement of this script.
    """
    body = (FIXTURE_ROOT / provider / "stream_success.sse").read_text(encoding="utf-8")
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


class ChaosTransport:
    """The far side of every provider call, with a schedule instead of a network.

    Health is looked up by the *round* the driver is currently in rather than by
    the clock, which is what makes a seed reproducible: two runs send the same
    request into the same fleet state even if one of them is on a busier machine.
    """

    def __init__(
        self,
        *,
        schedule: Sequence[Phase],
        hosts: dict[str, str],
        models_by_provider: dict[str, tuple[str, ...]],
    ) -> None:
        self._schedule = schedule
        self._hosts = hosts
        self._models = models_by_provider
        self.round = 0
        self.calls: Counter[str] = Counter()
        """Upstream calls per ``provider/model``, whatever the outcome."""

        self.refused: Counter[str] = Counter()
        """The subset of those that were answered with a failure."""

    def health(self, provider: str, model: str) -> Health:
        kills = phase_for_round(self._schedule, self.round).kills
        specific = kills.get(f"{provider}/{model}")
        if specific is not None:
            return specific
        return kills.get(provider, HEALTHY)

    def fleet(self) -> list[tuple[str, str, Health]]:
        """Every routable candidate and its state right now, for the live table."""
        return [
            (provider, model, self.health(provider, model))
            for provider, models in self._models.items()
            for model in models
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        provider = self._hosts.get(request.url.host)
        if provider is None:  # pragma: no cover - defensive
            raise AssertionError(f"no provider owns host {request.url.host!r}")

        model = _target_model(request)
        state = self.health(provider, model)
        self.calls[f"{provider}/{model}"] += 1
        if state != HEALTHY:
            self.refused[f"{provider}/{model}"] += 1
            return load_fixture(provider, FIXTURE_FOR[state])
        if _is_streaming(request):
            return load_stream(provider)
        return load_fixture(provider, "success")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _target_model(request: httpx.Request) -> str:
    """The model an outbound request names, whichever half of it carries one.

    Groq and OpenRouter put it in the body; Gemini puts it in the path and
    refuses it in the body, so a body-only reading would call every Gemini
    request a request for ``""`` and the per-candidate kills would never match.
    """
    body = _json_body(request)
    if body is not None:
        model = body.get("model")
        if isinstance(model, str) and model:
            return model

    segment = request.url.path.rsplit("/", 1)[-1]
    return segment.split(":", 1)[0] if ":" in segment else ""


def _is_streaming(request: httpx.Request) -> bool:
    if "streamGenerateContent" in request.url.path:
        return True
    body = _json_body(request)
    return bool(body.get("stream")) if body is not None else False


def _json_body(request: httpx.Request) -> dict[str, Any] | None:
    try:
        parsed = json.loads(request.content)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------------------- #
# The workload
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Call:
    """One planned request. Built up front so execution order cannot change it."""

    round: int
    client: int
    slot: str
    stream: bool
    prompt: str


def plan_workload(*, rounds: int, concurrency: int, seed: int) -> tuple[tuple[Call, ...], ...]:
    """Every request of the run, decided before the first one is sent.

    Planning up front rather than rolling a die per request is the difference
    between a seed that reproduces a run and a seed that reproduces a *sequence*
    of dice: under concurrency the second one is consumed in whatever order the
    event loop happens to resume its tasks.
    """
    rng = random.Random(seed)
    return tuple(
        tuple(
            Call(
                round=index,
                client=client,
                slot=rng.choice(SLOTS),
                stream=rng.random() < STREAM_FRACTION,
                prompt=f"chaos r{index} c{client} {rng.randrange(16**8):08x}",
            )
            for client in range(concurrency)
        )
        for index in range(rounds)
    )


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one request did, from the client's side of the wire only."""

    call: Call
    status: int
    latency_ms: float
    ok: bool
    provider: str | None = None
    model: str | None = None
    slot: str | None = None
    substituted: bool = False
    attempts: int = 0
    restarts: int = 0
    cache: str | None = None
    error_code: str | None = None


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
async def send(client: httpx.AsyncClient, call: Call, headers: dict[str, str]) -> Outcome:
    """One turn, as an SDK user would make it. Never raises."""
    body: dict[str, Any] = {
        "model": call.slot,
        "messages": [{"role": "user", "content": call.prompt}],
        "stream": call.stream,
        # Away from zero deliberately: D19 would answer a repeated prompt out of
        # the exact cache, and a demo whose provider calls quietly stopped
        # happening would report a very impressive zero failures.
        "temperature": 0.7,
    }
    started = time.perf_counter()
    try:
        if call.stream:
            return await _send_streaming(client, call, headers, body, started)
        response = await client.post(COMPLETIONS, json=body, headers=headers)
        return _read_completion(call, response, started)
    except Exception as exc:
        # A transport fault is a client-visible failure like any other, and the
        # summary's headline number would be a lie if one of these escaped.
        return Outcome(
            call=call,
            status=0,
            latency_ms=(time.perf_counter() - started) * 1000,
            ok=False,
            error_code=type(exc).__name__,
        )


def _read_completion(call: Call, response: httpx.Response, started: float) -> Outcome:
    latency_ms = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        return Outcome(
            call=call,
            status=response.status_code,
            latency_ms=latency_ms,
            ok=False,
            error_code=_error_code(response),
        )
    payload = response.json()
    served = payload["served_by"]
    return Outcome(
        call=call,
        status=200,
        latency_ms=latency_ms,
        ok=True,
        provider=served["provider"],
        model=served["model"],
        slot=served["slot"],
        substituted=bool(payload["substituted"]),
        attempts=int(payload["attempts"]),
        cache=response.headers.get("x-cache"),
    )


async def _send_streaming(
    client: httpx.AsyncClient,
    call: Call,
    headers: dict[str, str],
    body: dict[str, Any],
    started: float,
) -> Outcome:
    """A streamed turn, read to its terminal event.

    A stream that dies after the first byte is an HTTP 200 whose ``done`` event
    says ``status: "failed"`` — §1.1's contract, and the one client-visible
    failure this script would otherwise count as a success.
    """
    restarts = 0
    done: dict[str, Any] | None = None
    cache: str | None = None
    async with client.stream("POST", COMPLETIONS, json=body, headers=headers) as response:
        if response.status_code != 200:
            await response.aread()
            return Outcome(
                call=call,
                status=response.status_code,
                latency_ms=(time.perf_counter() - started) * 1000,
                ok=False,
                error_code=_error_code(response),
            )
        cache = response.headers.get("x-cache")
        event = ""
        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                if event == "restart":
                    restarts += 1
                elif event == "done":
                    done = json.loads(line.removeprefix("data: "))

    latency_ms = (time.perf_counter() - started) * 1000
    if done is None:
        return Outcome(
            call=call,
            status=200,
            latency_ms=latency_ms,
            ok=False,
            restarts=restarts,
            cache=cache,
            error_code="stream_truncated",
        )
    served = done["served_by"]
    succeeded = done["status"] == "ok"
    return Outcome(
        call=call,
        status=200,
        latency_ms=latency_ms,
        ok=succeeded,
        provider=served["provider"],
        model=served["model"],
        slot=served["slot"],
        substituted=bool(done["substituted"]),
        attempts=int(done["attempts"]),
        restarts=restarts,
        cache=cache,
        error_code=None if succeeded else "stream_failed",
    )


def _error_code(response: httpx.Response) -> str:
    """The gateway's own error code, when the body is its envelope."""
    with suppress(Exception):
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            if isinstance(code, str):
                return code
    return f"http_{response.status_code}"


# --------------------------------------------------------------------------- #
# Watching the breaker
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Transition:
    at_s: float
    provider: str
    model: str
    from_state: str
    to_state: str


class BreakerWatcher:
    """Polls ``CircuitBreaker.peek`` and records every state change it sees.

    ``peek`` and not ``allows`` for the reason that method exists (Phase 3 Step
    7): ``allows`` claims the one half-open probe slot, and a monitor that stole
    it from a real request would be changing the run it is meant to observe.
    """

    def __init__(
        self,
        breaker: CircuitBreaker,
        candidates: Sequence[tuple[str, str]],
        *,
        started: float,
    ) -> None:
        self._breaker = breaker
        self._candidates = tuple(candidates)
        self._started = started
        self._seen: dict[tuple[str, str], str] = {}
        self.states: dict[tuple[str, str], str] = {}
        self.transitions: list[Transition] = []

    async def poll(self) -> None:
        for provider, model in self._candidates:
            decision = await self._breaker.peek(provider, model)
            state = str(decision.state)
            self.states[(provider, model)] = state
            previous = self._seen.get((provider, model), "closed")
            if state != previous:
                self.transitions.append(
                    Transition(
                        at_s=round(time.perf_counter() - self._started, 2),
                        provider=provider,
                        model=model,
                        from_state=previous,
                        to_state=state,
                    )
                )
            self._seen[(provider, model)] = state

    async def run(self, stop: asyncio.Event, *, interval: float = 0.25) -> None:
        while not stop.is_set():
            await self.poll()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
        await self.poll()


# --------------------------------------------------------------------------- #
# The live table
# --------------------------------------------------------------------------- #
class LiveTable:
    """A block of lines rewritten in place, or plain appended lines when piped."""

    def __init__(self, *, live: bool) -> None:
        self._live = live and sys.stdout.isatty()
        self._lines = 0

    def render(self, lines: Sequence[str]) -> None:
        if self._live and self._lines:
            sys.stdout.write(f"\x1b[{self._lines}A")
        for line in lines:
            sys.stdout.write(("\x1b[2K" if self._live else "") + line + "\n")
        sys.stdout.flush()
        self._lines = len(lines)


def table_lines(
    *,
    phase: Phase,
    index: int,
    rounds: int,
    transport: ChaosTransport,
    watcher: BreakerWatcher,
    outcomes: Sequence[Outcome],
    in_flight: int,
) -> list[str]:
    served: Counter[str] = Counter(
        f"{o.provider}/{o.model}" for o in outcomes if o.provider is not None
    )
    latencies = sorted(o.latency_ms for o in outcomes)
    failures = sum(1 for o in outcomes if not o.ok)
    substitutions = sum(1 for o in outcomes if o.substituted)

    lines = [
        f"round {index + 1:>4}/{rounds}   phase: {phase.name} - {phase.note}",
        f"requests {len(outcomes):>5}   in flight {in_flight:>2}   "
        f"client-visible failures {failures}   substitutions {substitutions}   "
        f"p50 {_percentile(latencies, 50):.0f}ms",
        "",
        f"  {'candidate':<50}{'upstream':<10}{'breaker':<11}served",
    ]
    for provider, model, state in transport.fleet():
        key = f"{provider}/{model}"
        breaker_state = watcher.states.get((provider, model), "closed")
        lines.append(f"  {key:<50}{STATE_GLYPH[state]:<10}{breaker_state:<11}{served[key]}")
    return lines


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, round((pct / 100) * (len(sorted_values) - 1)))
    return sorted_values[index]


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def _apply_env_overrides(args: argparse.Namespace) -> None:
    """Set the switches this demo needs, before anything reads ``Settings``.

    Every one of these already exists and is already documented; see the module
    docstring for why each is set the way it is. ``os.environ`` rather than a
    hand-built ``Settings`` because the app reads its configuration through one
    ``lru_cache``d accessor, which the caller clears right after this.
    """
    os.environ["RATE_LIMIT_ENABLED"] = "false"
    os.environ["ROUTING_LATENCY_RANKING"] = "false"
    os.environ["FILES_STORAGE_BACKEND"] = "memory"
    if not args.quota:
        os.environ["QUOTA_ENFORCEMENT"] = "false"
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if args.redis_url:
        os.environ["REDIS_URL"] = args.redis_url


def open_redis(settings: Settings, *, real: bool) -> Redis:
    """``fakeredis`` unless a URL was given, so a run starts from a clean fleet."""
    if real:
        from app.cache.client import create_redis_client

        return create_redis_client(settings)

    try:
        from fakeredis.aioredis import FakeRedis
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
        raise SystemExit(
            "fakeredis is not installed (it ships with the dev extra). "
            'Either `pip install -e ".[dev]"` or pass --redis-url.'
        ) from exc
    return FakeRedis(decode_responses=True)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Stand the app up, drive it through the schedule, tear it down."""
    _apply_env_overrides(args)

    from app.auth.api_keys import generate_api_key
    from app.auth.jwt import JwksCache
    from app.cache.client import LuaScriptRegistry
    from app.config import get_providers_config, get_settings
    from app.core.logging import configure_logging
    from app.db.models import ApiKey, User
    from app.db.session import create_db_engine, create_session_factory
    from app.main import create_app
    from app.perception.storage import MemoryStore
    from app.providers.registry import build_registry
    from app.routing.circuit_breaker import CircuitBreaker
    from app.usage.metrics import LatencyTable, MetricsRegistry

    get_settings.cache_clear()
    settings = get_settings()
    app = create_app()
    # `create_app` installs the INFO pipeline, and a demo whose own table is
    # being overwritten by JSON log lines is unreadable. --log-level turns it
    # back up for anyone debugging the script rather than watching it.
    configure_logging(args.log_level)

    rounds = max(1, int(args.duration / args.interval))
    schedule = build_schedule(rounds, concurrency=args.concurrency, interval=args.interval)
    workload = plan_workload(rounds=rounds, concurrency=args.concurrency, seed=args.seed)

    providers_config = get_providers_config()
    hosts = {
        urlsplit(entry.base_url).hostname or "": name
        for name, entry in providers_config.providers.items()
        if entry.enabled
    }
    candidates = routable_candidates(providers_config)

    # One client, one transport: the registry the app routes through and the
    # scripted upstream are the same object, which is what makes "kill a
    # provider" a dictionary lookup rather than a network trick.
    transport = ChaosTransport(
        schedule=schedule,
        hosts=hosts,
        models_by_provider={
            provider: tuple(model for owner, model in candidates if owner == provider)
            for provider in dict.fromkeys(provider for provider, _ in candidates)
        },
    )
    upstream = transport.client()
    registry = build_registry(client=upstream, settings=settings)

    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    redis = open_redis(settings, real=bool(args.redis_url))

    # Everything the lifespan would build, supplied by hand — `ASGITransport`
    # does not run it, and we would not want it to: it would open a real HTTP
    # client and this demo's whole point is that it does not have one.
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    app.state.http_client = upstream
    # Never actually fetched — every request here presents a `gw_live_` key, and
    # `get_principal` short-circuits before it asks for a signing key. It is set
    # because that dependency reads the attribute before it branches, and an
    # unset one is a 500 on the very first request.
    app.state.jwks_cache = JwksCache(jwks_url=settings.supabase_jwks_url, client=upstream)
    app.state.provider_registry = registry
    app.state.object_store = MemoryStore()
    app.state.redis = redis
    scripts = LuaScriptRegistry(redis)
    scripts.load_dir()
    await scripts.warm()
    app.state.lua_scripts = scripts
    app.state.latency = LatencyTable()
    app.state.metrics = MetricsRegistry()

    generated = generate_api_key()
    user_id = uuid4()
    async with session_factory() as session:
        session.add(
            User(
                id=user_id,
                email=f"chaos-{user_id.hex[:12]}@example.invalid",
                email_verified=True,
                tier="free",
            )
        )
        # Flushed before the key: the unit of work orders inserts by mapper, not
        # by the order they were added, and the api_keys FK needs the row first.
        await session.flush()
        session.add(
            ApiKey(
                id=uuid4(),
                user_id=user_id,
                key_hash=generated.key_hash,
                key_prefix=generated.key_prefix,
                last_4=generated.last_4,
                nickname="chaos-demo",
            )
        )
        await session.commit()

    # `X-API-Key`, not `Authorization` — that header is the Supabase-session
    # half of D7, and a `gw_live_` key presented there is a 401 by design.
    headers = {"X-API-Key": generated.plaintext}
    outcomes: list[Outcome] = []
    started = time.perf_counter()
    wall_clock = 0.0
    watcher = BreakerWatcher(CircuitBreaker(redis), candidates, started=started)
    stop = asyncio.Event()
    monitor = asyncio.create_task(watcher.run(stop))
    table = LiveTable(live=not args.no_live)
    in_flight = 0

    gateway = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://chaos-demo",
        timeout=60.0,
    )

    async def _one(call: Call) -> Outcome:
        nonlocal in_flight
        in_flight += 1
        try:
            return await send(gateway, call, headers)
        finally:
            in_flight -= 1

    try:
        for index, calls in enumerate(workload):
            transport.round = index
            deadline = started + (index + 1) * args.interval
            outcomes.extend(await asyncio.gather(*(_one(call) for call in calls)))
            table.render(
                table_lines(
                    phase=phase_for_round(schedule, index),
                    index=index,
                    rounds=rounds,
                    transport=transport,
                    watcher=watcher,
                    outcomes=outcomes,
                    in_flight=in_flight,
                )
            )
            remaining = deadline - time.perf_counter()
            if remaining > 0:
                await asyncio.sleep(remaining)
        wall_clock = time.perf_counter() - started
    finally:
        stop.set()
        await monitor
        await gateway.aclose()
        await upstream.aclose()
        if not args.keep_data:
            async with session_factory() as session:
                user = await session.get(User, user_id)
                if user is not None:
                    await session.delete(user)
                    await session.commit()
        await redis.aclose()
        await engine.dispose()

    return summarize(
        args=args,
        schedule=schedule,
        rounds=rounds,
        outcomes=outcomes,
        transport=transport,
        watcher=watcher,
        wall_clock_s=wall_clock,
        user_id=user_id if args.keep_data else None,
    )


# --------------------------------------------------------------------------- #
# The summary — the artifact this script exists to produce
# --------------------------------------------------------------------------- #
def summarize(
    *,
    args: argparse.Namespace,
    schedule: Sequence[Phase],
    rounds: int,
    outcomes: Sequence[Outcome],
    transport: ChaosTransport,
    watcher: BreakerWatcher,
    wall_clock_s: float,
    user_id: UUID | None,
) -> dict[str, Any]:
    """Two blocks: what the run was *asked* to do, and what actually happened.

    ``plan`` is a pure function of the seed and the schedule — the same seed
    produces a byte-identical block, which is what makes a recorded run checkable
    against a fresh one. ``outcome`` is measured, and the honest reading is that
    only some of it is stable: the headline (every request answered, nothing
    client-visible) reproduces, while the per-candidate counts move by a request
    or two between runs, because a breaker's cooldown expires against a wall
    clock and a request that arrives either side of that boundary is routed
    differently. Splitting them here rather than claiming the whole summary is
    reproducible is the same disclosure discipline the gateway applies to its own
    answers.
    """
    latencies = sorted(o.latency_ms for o in outcomes)
    killed = sorted({target for phase in schedule for target in phase.kills})

    phases: list[dict[str, Any]] = []
    cursor = 0
    for phase in schedule:
        phases.append(
            {
                "name": phase.name,
                "note": phase.note,
                "first_round": cursor,
                "last_round": cursor + phase.rounds - 1,
                "kills": dict(sorted(phase.kills.items())),
            }
        )
        cursor += phase.rounds

    plan: dict[str, Any] = {
        "seed": args.seed,
        "rounds": rounds,
        "concurrency": args.concurrency,
        "interval_s": args.interval,
        "requests": len(outcomes),
        "streamed": sum(1 for o in outcomes if o.call.stream),
        "slot_mix": dict(sorted(Counter(o.call.slot for o in outcomes).items())),
        "phases": phases,
        "targets_killed": killed,
    }

    outcome: dict[str, Any] = {
        "wall_clock_s": round(wall_clock_s, 1),
        "statuses": dict(sorted(Counter(str(o.status) for o in outcomes).items())),
        "client_visible_failures": sum(1 for o in outcomes if not o.ok),
        "failure_codes": dict(
            sorted(Counter([o.error_code for o in outcomes if o.error_code is not None]).items())
        ),
        # In `outcome` and not in `plan`, and the distinction is real: a spill
        # into another slot also happens whenever `general`'s chain is behind
        # open breakers, and those close on a wall clock. Two runs of one seed
        # differ here by a request or two, at the edge of a cooldown.
        "substitutions_disclosed": sum(1 for o in outcomes if o.substituted),
        "cache_hits": sum(1 for o in outcomes if o.cache == "HIT"),
        "served_by_provider": dict(
            sorted(Counter([o.provider for o in outcomes if o.provider is not None]).items())
        ),
        "served_by_candidate": dict(
            sorted(
                Counter(
                    [f"{o.provider}/{o.model}" for o in outcomes if o.provider is not None]
                ).items()
            )
        ),
        "upstream_calls": dict(sorted(transport.calls.items())),
        "upstream_refusals": dict(sorted(transport.refused.items())),
        "attempts": dict(sorted(Counter(str(o.attempts) for o in outcomes).items())),
        "multi_attempt": sum(1 for o in outcomes if o.attempts > 1),
        "stream_restarts": sum(o.restarts for o in outcomes),
        "breaker_transitions": [
            {
                "at_s": t.at_s,
                "provider": t.provider,
                "model": t.model,
                "from": t.from_state,
                "to": t.to_state,
            }
            for t in watcher.transitions
        ],
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 1),
            "p95": round(_percentile(latencies, 95), 1),
            "max": round(latencies[-1], 1) if latencies else 0.0,
        },
    }

    return {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "user_id": str(user_id) if user_id is not None else None,
        "plan": plan,
        "outcome": outcome,
    }


def print_summary(summary: dict[str, Any]) -> None:
    plan: dict[str, Any] = summary["plan"]
    outcome: dict[str, Any] = summary["outcome"]
    rule = "=" * 78

    print()
    print(rule)
    print("  chaos demo - summary")
    print(rule)
    print(
        f"  seed {plan['seed']}   {plan['rounds']} rounds x "
        f"{plan['concurrency']} clients @ {plan['interval_s']}s   "
        f"({outcome['wall_clock_s']}s wall clock)"
    )
    print()
    print(f"  requests sent ................. {plan['requests']} ({plan['streamed']} streamed)")
    print(
        f"  targets killed ................ {len(plan['targets_killed'])} "
        f"({', '.join(plan['targets_killed'])})"
    )
    print(f"  CLIENT-VISIBLE FAILURES ...... {outcome['client_visible_failures']}")
    print(f"  substitutions disclosed ...... {outcome['substitutions_disclosed']}")
    print(f"  multi-attempt turns .......... {outcome['multi_attempt']}")
    print(f"  stream restarts .............. {outcome['stream_restarts']}")
    print(f"  cache hits ................... {outcome['cache_hits']}")
    print(f"  statuses ..................... {outcome['statuses']}")
    if outcome["failure_codes"]:
        print(f"  failure codes ................ {outcome['failure_codes']}")
    print()
    print("  served by candidate:")
    for candidate, count in outcome["served_by_candidate"].items():
        print(f"    {candidate:<46}{count}")
    print()
    print("  breaker transitions:")
    if not outcome["breaker_transitions"]:
        print("    (none - no candidate failed often enough to open one)")
    for transition in outcome["breaker_transitions"]:
        print(
            f"    t+{transition['at_s']:>6.2f}s  {transition['provider']}/{transition['model']}"
            f"  {transition['from']} -> {transition['to']}"
        )
    print()
    latency = outcome["latency_ms"]
    print(
        "  latency (in-process, not a network): "
        f"p50 {latency['p50']}ms  p95 {latency['p95']}ms  max {latency['max']}ms"
    )
    print(rule)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.chaos_demo",
        description="Drive the real gateway under load while its providers die and recover.",
    )
    parser.add_argument("--duration", type=float, default=90.0, help="seconds of load (default 90)")
    parser.add_argument("--concurrency", type=int, default=4, help="concurrent clients (default 4)")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds per round (default 1)")
    parser.add_argument("--seed", type=int, default=1, help="workload seed (default 1)")
    parser.add_argument("--json", dest="json_out", type=Path, help="write the summary to a file")
    parser.add_argument("--redis-url", default=None, help="a real Redis instead of fakeredis")
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument(
        "--quota",
        action="store_true",
        help="leave QUOTA_ENFORCEMENT on (free-tier windows exhaust in seconds)",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="leave the demo user and its rows behind (for the /usage dashboard)",
    )
    parser.add_argument("--no-live", action="store_true", help="plain output, no cursor tricks")
    parser.add_argument("--log-level", default="error", help="gateway log level (default error)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.concurrency < 1 or args.duration <= 0 or args.interval <= 0:
        print("--concurrency, --duration and --interval must all be positive", file=sys.stderr)
        return 2

    summary = asyncio.run(run(args))
    print_summary(summary)
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"\n  wrote {args.json_out}")
    # A non-zero exit on a client-visible failure, so this is usable as a check
    # rather than only as a demo.
    return 1 if summary["outcome"]["client_visible_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
