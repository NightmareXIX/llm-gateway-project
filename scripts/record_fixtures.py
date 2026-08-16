"""Capture real provider responses once, so tests never need a live API.

The hard rule this exists to serve: **no test ever calls a provider.** Live calls
in CI burn a shared free-tier budget, fail when a provider has a bad afternoon,
and turn an assertion about error parsing into an assertion about the internet.
So the real responses get recorded here, committed, and replayed forever after
through ``httpx.MockTransport``.

Run it while you have working keys::

    make record-fixtures                                  # every provider, everything provokable
    make record-fixtures ARGS="--provider gemini"          # one provider
    make record-fixtures ARGS="--force"                    # overwrite existing live captures

Each case writes ``tests/fixtures/provider_responses/<provider>/<name>.json``
holding the status, the headers and the body. **Headers are not optional
decoration** — ``retry-after`` and ``x-ratelimit-*`` are the inputs to
``rate_limit_headers`` and the retry hint on a 429, so a fixture without them
cannot test the code paths that read them. Gemini is the instructive exception: it
publishes none, and the recorded absence is what its ``rate_limit_headers`` test
asserts against.

**One recipe per provider, no branches in the loop.** Everything the script knows
about a provider lives in its :class:`Recipe` — which key to read, which header
carries it, where the model goes, what can be provoked. :func:`record` knows about
none of them, which is the same split the adapters themselves use.

Two categories are deliberately never recorded and stay ``source: "synthetic"``:

- **429s, 413-TPM and OpenRouter's 402** — provoking these means deliberately
  exhausting a shared budget or spending down an account, which is worse than
  hand-writing the body.
- **empty, blocked and content-filtered 200s** — not reachable on demand at all.

Each recipe lists its own, and the closing summary prints them.

The script refuses to overwrite a ``source: "live"`` file without ``--force``, so
a partial run cannot silently downgrade a real capture to a placeholder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from app.config import REPO_ROOT, ProviderEntry, get_providers_config, get_settings

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "provider_responses"

REDACTED_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "x-goog-api-key", "cookie", "set-cookie"}
)
"""Never written to a committed file. The whole directory goes into git.

Matched on exact lowercased names, so every header a provider might authenticate
with has to be listed individually — ``x-goog-api-key`` is not covered by
``api-key``. The conformance suite's leak test is the backstop for anything missed
here, including a key echoed back inside a *body*, which this cannot see.
"""

VOLATILE_HEADERS = frozenset(
    {"date", "cf-ray", "cf-cache-status", "alt-svc", "server", "vary", "connection"}
)
"""Dropped because they change every run and would make every re-record a diff."""

TRANSPORT_HEADERS = frozenset({"content-encoding", "transfer-encoding", "content-length"})
"""Dropped because they describe the *original* wire framing, not the response.

``httpx`` hands back a decoded body, so a fixture that keeps ``content-encoding:
gzip`` next to plain JSON tells the replaying client to gunzip something that was
already gunzipped — every test using that fixture then fails with a decode error
several layers from the cause. Same for a ``content-length`` that no longer matches
and a ``transfer-encoding`` that no longer applies. These are the only headers
dropped for correctness rather than for tidiness.
"""


@dataclass(frozen=True)
class Case:
    """One request to make and the file to write it to."""

    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    use_valid_key: bool = True
    note: str = ""


@dataclass(frozen=True)
class Recipe:
    """Everything the script knows about one provider.

    ``cases`` takes the model name because two of the provokable cases embed it —
    and for Gemini the model is in the *path*, which is why the whole ``Case`` is
    built by the recipe rather than assembled generically from a template.
    """

    provider: str
    key_field: str
    """A ``Settings`` attribute name — the same one ``providers.yaml`` declares as
    ``api_key_env``, so a mismatch here is caught by the registry at boot."""

    placeholder_prefixes: tuple[str, ...]
    """Prefixes that mean "this is the .env.example value, refuse to run"."""

    invalid_key: str
    """The syntactically plausible but dead key that provokes the auth failure.

    Written into a committed fixture whenever a provider echoes it back, so each of
    these must match a sentinel in ``tests/contract/test_adapter_conformance.py``'s
    ``CREDENTIAL_PREFIXES``. The two cannot import each other — a script depending
    on the test package would make recording depend on the suite — so they are
    kept in step by hand, and the leak test fails loudly if they drift.
    """

    auth: Callable[[str, ProviderEntry], dict[str, str]]
    cases: Callable[[str], list[Case]]
    synthetic_only: tuple[str, ...] = ()
    """Fixture names this provider cannot provoke, printed in the closing summary."""

    extra_notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Groq — OpenAI-shaped, Bearer auth, model in the body
# --------------------------------------------------------------------------- #
def _openai_cases(model: str, *, missing_model: str) -> list[Case]:
    """The provokable set for any OpenAI-shaped provider."""
    return [
        Case(
            name="success",
            method="POST",
            path="/chat/completions",
            body={
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are terse."},
                    {"role": "user", "content": "In one sentence: what is an LLM gateway?"},
                ],
                "temperature": 0.0,
                "max_tokens": 64,
            },
            note="The happy path. Also the source of truth for the ±25% estimate check.",
        ),
        Case(
            name="models_list",
            method="GET",
            path="/models",
            note="What validate_key calls. Costs no completion quota.",
        ),
        Case(
            name="auth_failed",
            method="POST",
            path="/chat/completions",
            body={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            use_valid_key=False,
            note="Provoked with a syntactically plausible but invalid key.",
        ),
        Case(
            name="bad_request",
            method="POST",
            path="/chat/completions",
            # `role` omitted on purpose — this is what our own bug looks like.
            body={"model": model, "messages": [{"content": "hi"}], "max_tokens": 1},
            note="Provoked with a malformed message object.",
        ),
        Case(
            name="model_not_found",
            method="POST",
            path="/chat/completions",
            body={
                "model": missing_model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            note="Provoked by naming a model the provider does not serve.",
        ),
    ]


def _bearer(key: str, entry: ProviderEntry) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _openrouter_auth(key: str, entry: ProviderEntry) -> dict[str, str]:
    """Bearer plus the attribution headers, so the recording exercises the same
    header set the adapter sends."""
    headers = dict(entry.options)
    headers["Authorization"] = f"Bearer {key}"
    return headers


GROQ = Recipe(
    provider="groq",
    key_field="GROQ_API_KEY",
    placeholder_prefixes=("gsk_test", "gsk_replace_me"),
    invalid_key="gsk_deliberately_invalid_key_for_fixtures",
    auth=_bearer,
    cases=lambda model: _openai_cases(model, missing_model="llama-3.1-405b-reasoning"),
    synthetic_only=(
        "rate_limited",
        "rate_limited_tpm_413",
        "empty_response",
        "content_filtered",
        "server_error_html",
        "stream_success.sse",
        "stream_truncated.sse",
    ),
)


# --------------------------------------------------------------------------- #
# Gemini — model in the path, key in x-goog-api-key, its own body shape
# --------------------------------------------------------------------------- #
def _gemini_cases(model: str) -> list[Case]:
    generate = f"/models/{model}:generateContent"
    return [
        Case(
            name="success",
            method="POST",
            path=generate,
            body={
                "system_instruction": {"parts": [{"text": "You are terse."}]},
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": "In one sentence: what is an LLM gateway?"}],
                    }
                ],
                # 512, not the 64 the OpenAI-shaped recipe uses. Gemini 3 models
                # think before answering and bill those tokens against the same
                # output budget, so a 64-token cap spends the lot on reasoning and
                # returns one word with `finishReason: MAX_TOKENS` — a fixture that
                # is technically a 200 and useless as the happy path.
                "generationConfig": {"temperature": 0.0, "maxOutputTokens": 512},
            },
            note=(
                "The happy path. Also the source of truth for the ±25% estimate check. "
                "Recorded with a 512-token output budget because this model thinks "
                "first and a smaller cap never reaches an answer."
            ),
        ),
        Case(
            name="models_list",
            method="GET",
            path="/models",
            note="What validate_key calls. Costs no completion quota.",
        ),
        Case(
            name="api_key_invalid",
            method="POST",
            path=generate,
            body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            use_valid_key=False,
            note=(
                "The 400-not-401 quirk. Provoked with a plausible but dead key — "
                "Google answers INVALID_ARGUMENT, which the naive status ladder "
                "would classify as a failover-ineligible BadRequest."
            ),
        ),
        Case(
            name="bad_request",
            method="POST",
            path=generate,
            # An OpenAI-shaped body: what trap 10 looks like on the wire.
            body={"messages": [{"role": "user", "content": "hi"}]},
            note="Provoked by sending an OpenAI-shaped body to Gemini.",
        ),
        Case(
            name="model_not_found",
            method="POST",
            path="/models/gemini-9-ultra:generateContent",
            body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            note="Provoked by naming a model that does not exist.",
        ),
    ]


GEMINI = Recipe(
    provider="gemini",
    key_field="GEMINI_API_KEY",
    placeholder_prefixes=("gemini_test", "replace_me"),
    invalid_key="AIzaSyDeliberatelyInvalidKeyForFixtures",
    # Not `?key=`, matching the adapter: a URL ends up in access logs on every hop.
    auth=lambda key, _entry: {"x-goog-api-key": key},
    cases=_gemini_cases,
    synthetic_only=(
        "rate_limited",
        "auth_failed",
        "unavailable",
        "empty_response",
        "blocked_prompt",
        "finish_reason_safety",
        "success_no_usage",
        "server_error_html",
        "stream_success.sse",
        "stream_truncated.sse",
    ),
    extra_notes=(
        "auth_failed (403 PERMISSION_DENIED) needs a key from a project without the "
        "API enabled, which is not something a recording run can arrange.",
    ),
)


# --------------------------------------------------------------------------- #
# OpenRouter — OpenAI-shaped, plus attribution headers
# --------------------------------------------------------------------------- #
OPENROUTER = Recipe(
    provider="openrouter",
    key_field="OPENROUTER_API_KEY",
    placeholder_prefixes=("sk-or-v1-test", "sk-or-v1-replace_me"),
    invalid_key="sk-or-v1-deliberately-invalid-key-for-fixtures",
    auth=_openrouter_auth,
    cases=lambda model: _openai_cases(model, missing_model="meta-llama/llama-3-70b:free"),
    synthetic_only=(
        "rate_limited",
        "credits_exhausted",
        "moderated",
        "empty_response",
        "content_filtered",
        "error_body_200",
        "server_error_html",
        "stream_success.sse",
        "stream_truncated.sse",
    ),
    extra_notes=(
        "credits_exhausted (402) would mean spending an account down to zero, and "
        "moderated (403) means deliberately submitting content that trips an "
        "upstream's filter. Both stay hand-written.",
    ),
)


RECIPES: tuple[Recipe, ...] = (GROQ, GEMINI, OPENROUTER)


_DROPPED_HEADERS = REDACTED_HEADERS | VOLATILE_HEADERS | TRANSPORT_HEADERS


def _clean_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _DROPPED_HEADERS}


def _payload(case: Case, response: httpx.Response) -> dict[str, Any]:
    record: dict[str, Any] = {
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "live",
        "note": case.note,
        # The body is recorded, not just the method and path. Without it a fixture
        # cannot say which prompt produced the usage numbers next to it — and the
        # conformance suite's estimate check compares `estimate_tokens` against
        # exactly that, so a missing body turns a real assertion into a comparison
        # between two unrelated prompts.
        "request": {"method": case.method, "path": case.path, "body": case.body},
        "response": {
            "status": response.status_code,
            "headers": _clean_headers(response.headers),
        },
    }

    try:
        parsed = response.json()
    except ValueError:
        record["response"]["text"] = response.text
        return record

    if isinstance(parsed, dict):
        record["response"]["body"] = parsed
    else:
        record["response"]["text"] = response.text
    return record


def _may_write(path: Path, *, force: bool) -> bool:
    """Refuse to clobber a real capture with a partial run's output."""
    if force or not path.exists():
        return True
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return bool(existing.get("source") != "live")


def _resolve_key(recipe: Recipe) -> str | None:
    """The recipe's key, or ``None`` if it is still the placeholder.

    A placeholder skips that provider rather than aborting the run: with three
    providers, "I only have a Groq key today" is the normal case, and failing the
    whole invocation over it would make the script useless exactly when it is most
    convenient to use.
    """
    settings = get_settings()
    # `getattr` because the field name comes from the recipe, matching how
    # `registry._resolve_system_key` resolves the same name from `providers.yaml`.
    # The isinstance narrows it back from `Any`, and covers the case where the
    # attribute exists but is not a secret.
    secret = getattr(settings, recipe.key_field, None)
    if not isinstance(secret, SecretStr):  # pragma: no cover — the registry checks this at boot
        print(f"  Settings has no SecretStr field named {recipe.key_field}")
        return None

    key = secret.get_secret_value()
    if not key.strip() or key.startswith(recipe.placeholder_prefixes):
        print(f"  {recipe.key_field} looks like a placeholder. Set a real key in .env first.")
        return None
    return key


async def record_one(
    recipe: Recipe, *, client: httpx.AsyncClient, force: bool, only: str | None
) -> int:
    """Record one provider's fixtures. Returns the number of failures."""
    providers = get_providers_config()
    entry = providers.providers[recipe.provider]
    model = next(
        candidate.model
        for slot in providers.slots.values()
        for candidate in slot.candidates
        if candidate.provider == recipe.provider
    )

    key = _resolve_key(recipe)
    if key is None:
        return 0

    out_dir = FIXTURE_ROOT / recipe.provider
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = [case for case in recipe.cases(model) if only is None or case.name == only]
    if not selected:
        print(f"  no case named {only!r}")
        return 0

    failures = 0
    for case in selected:
        destination = out_dir / f"{case.name}.json"
        if not _may_write(destination, force=force):
            print(f"  {case.name:<20} SKIP (already a live capture; --force to replace)")
            continue

        try:
            response = await client.request(
                case.method,
                f"{entry.base_url.rstrip('/')}{case.path}",
                headers=recipe.auth(key if case.use_valid_key else recipe.invalid_key, entry),
                json=case.body,
            )
        except httpx.HTTPError as exc:
            print(f"  {case.name:<20} FAIL ({type(exc).__name__}: {exc})")
            failures += 1
            continue

        destination.write_text(
            json.dumps(_payload(case, response), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  {case.name:<20} ok ({response.status_code}) -> {destination.name}")

    if recipe.synthetic_only:
        print(f"  stays synthetic: {', '.join(recipe.synthetic_only)}")
    for note in recipe.extra_notes:
        print(f"  note: {note}")
    return failures


async def record(*, recipes: tuple[Recipe, ...], force: bool, only: str | None) -> int:
    failures = 0
    # All four explicitly: httpx rejects a partial Timeout without a default, and
    # the partial form is what this script shipped with until the first person
    # actually ran it. Mirrors the adapters' own defaults in `providers/base.py`.
    timeout = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for recipe in recipes:
            print(f"\n{recipe.provider} -> {FIXTURE_ROOT / recipe.provider}")
            failures += await record_one(recipe, client=client, force=force, only=only)

    print("\nSee this script's docstring for why the synthetic ones stay synthetic.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing live captures")
    parser.add_argument("--only", metavar="CASE", help="record a single case by name")
    parser.add_argument(
        "--provider",
        metavar="NAME",
        choices=[recipe.provider for recipe in RECIPES],
        help="record one provider instead of all of them",
    )
    args = parser.parse_args()

    recipes = (
        RECIPES
        if args.provider is None
        else tuple(recipe for recipe in RECIPES if recipe.provider == args.provider)
    )
    return asyncio.run(record(recipes=recipes, force=args.force, only=args.only))


if __name__ == "__main__":
    sys.exit(main())
