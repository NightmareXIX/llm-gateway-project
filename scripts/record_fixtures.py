"""Capture real provider responses once, so tests never need a live API.

The hard rule this exists to serve: **no test ever calls a provider.** Live calls
in CI burn a shared free-tier budget, fail when a provider has a bad afternoon,
and turn an assertion about error parsing into an assertion about the internet.
So the real responses get recorded here, committed, and replayed forever after
through ``httpx.MockTransport``.

Run it while you have a working key::

    make record-fixtures                 # everything provokable
    make record-fixtures ARGS="--force"  # overwrite existing live captures

Each case writes ``tests/fixtures/provider_responses/<provider>/<name>.json``
holding the status, the headers and the body. **Headers are not optional
decoration** — ``retry-after`` and ``x-ratelimit-*`` are the inputs to
``rate_limit_headers`` and the retry hint on a 429, so a fixture without them
cannot test the code paths that read them.

Two categories are deliberately never recorded and stay ``source: "synthetic"``:

- **429 / 413-TPM** — provoking these means deliberately exhausting a shared
  budget, which is worse than hand-writing the body.
- **empty and content-filtered 200s** — not reachable on demand at all.

The script refuses to overwrite a ``source: "live"`` file without ``--force``, so
a partial run cannot silently downgrade a real capture to a placeholder.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import REPO_ROOT, get_providers_config, get_settings

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "provider_responses"

REDACTED_HEADERS = frozenset({"authorization", "x-api-key", "api-key", "cookie", "set-cookie"})
"""Never written to a committed file. The whole directory goes into git."""

VOLATILE_HEADERS = frozenset(
    {"date", "cf-ray", "cf-cache-status", "alt-svc", "server", "vary", "connection"}
)
"""Dropped because they change every run and would make every re-record a diff."""


@dataclass(frozen=True)
class Case:
    """One request to make and the file to write it to."""

    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    use_valid_key: bool = True
    note: str = ""


def cases(model: str) -> list[Case]:
    """Everything that can be provoked with one working key and no quota damage."""
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
                "model": "llama-3.1-405b-reasoning",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            note="Provoked by naming a decommissioned model.",
        ),
    ]


def _clean_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in REDACTED_HEADERS and name.lower() not in VOLATILE_HEADERS
    }


def _payload(case: Case, response: httpx.Response) -> dict[str, Any]:
    record: dict[str, Any] = {
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "live",
        "note": case.note,
        "request": {"method": case.method, "path": case.path},
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


async def record(*, force: bool, only: str | None) -> int:
    settings = get_settings()
    providers = get_providers_config()

    entry = providers.providers["groq"]
    model = next(
        candidate.model
        for slot in providers.slots.values()
        for candidate in slot.candidates
        if candidate.provider == "groq"
    )

    out_dir = FIXTURE_ROOT / "groq"
    out_dir.mkdir(parents=True, exist_ok=True)

    valid_key = settings.GROQ_API_KEY.get_secret_value()
    if not valid_key.strip() or valid_key.startswith("gsk_test"):
        print("GROQ_API_KEY looks like a placeholder. Set a real key in .env first.")
        return 2

    selected = [c for c in cases(model) if only is None or c.name == only]
    if not selected:
        print(f"no case named {only!r}")
        return 2

    failures = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=60.0)) as client:
        for case in selected:
            destination = out_dir / f"{case.name}.json"
            if not _may_write(destination, force=force):
                print(f"  {case.name:<20} SKIP (already a live capture; --force to replace)")
                continue

            key = valid_key if case.use_valid_key else "gsk_deliberately_invalid_key_for_fixtures"
            try:
                response = await client.request(
                    case.method,
                    f"{entry.base_url.rstrip('/')}{case.path}",
                    headers={"Authorization": f"Bearer {key}"},
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

    print(
        "\nNot recorded, by design — rate_limited, rate_limited_tpm_413, empty_response, "
        "content_filtered, server_error_html and the two .sse files stay synthetic.\n"
        "See this script's docstring for why."
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing live captures")
    parser.add_argument("--only", metavar="CASE", help="record a single case by name")
    args = parser.parse_args()

    print(f"recording groq fixtures into {FIXTURE_ROOT / 'groq'}\n")
    return asyncio.run(record(force=args.force, only=args.only))


if __name__ == "__main__":
    sys.exit(main())
