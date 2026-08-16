# Limitations

The honest-edges document. Not a bug list — a record of what was deliberately scoped out, what a free
tier costs you no matter how carefully the gateway is built, and what "works" means here versus what it
would mean in a paid, production system. Opened in Phase 2, because streaming and mid-stream failover
are where the free-tier trade-offs first become visible to a user rather than just to a log line.

---

## Streaming and failover

**A restart discards tokens the free tier already charged.** When D1's restart fires, the failed
candidate generated real output — tokens a provider's own metering counted against RPD/TPM — and the
gateway throws the text away because it produced no usable answer. `wasted_tokens_out` records this
honestly rather than hiding it (`ADR-012`), but recording a cost is not the same as avoiding it: a
message that triggers two restarts before succeeding has spent roughly three times the quota of one
that did not, on a pool that was already the scarce resource this whole project exists to manage.

**Two attempts on very different free models can produce visibly different answers.** `served_by` and
`substituted` disclose *which* model answered, and a `restart` event discloses that a swap happened
mid-generation — but disclosure is not consistency. A response that started in one model's voice and
finished in another's, after a restart, reads as coherent to a human ear at the sentence level and
inconsistent at the paragraph level in a way this project does not attempt to smooth over. The dialogue
this project is built to demonstrate is "what happens when a provider is slow but not fully down," not
"how do you make three different free models sound like one."

**A sleeping free-tier Fly instance can drop a stream mid-flight.** `fly.toml`'s
`auto_stop_machines = "suspend"` is accepted deliberately (`development-plan.md` §5's risk register) for
the cold-start cost it buys back on every quiet interval. The failure mode specific to streaming: a
machine that is asked to suspend while a long-running SSE response is still open can end that response
before `done` is ever sent, and the client sees a connection drop rather than an in-band failure — the
one shape of failure `route_stream`'s error hierarchy cannot classify, because nothing about it comes
from a provider. `min_machines_running = 0` is the setting to raise first if this becomes a recurring
demo problem; it costs the free tier's idle-suspend savings to fix.

**`auto`'s latency ranking leans toward the provider nearest its own limit, until Phase 3.** `ADR-014`
names this as the standing caveat it inherited from D11: ranking by measured speed with no quota
awareness preferentially selects whichever provider is fastest *because* it has the least contention —
which on a free tier is often the one closest to a 429. `ROUTING_LATENCY_RANKING` exists specifically so
this can be switched off in one deploy without a revert, and Phase 3's quota filter is the actual fix,
not a workaround.

**Restarting a stream is not free even when it never fires.** The first-token budget (`D13`,
`DEFAULT_FIRST_TOKEN_TIMEOUT_S = 10.0`) means a client's very first byte of *any* streamed response can
legitimately take up to 10 seconds if the first candidate is slow to start, before the gateway has even
decided whether a restart will be necessary. That is a deliberate trade against a worse alternative
(silence with no way to distinguish a slow provider from a dead gateway), not a cost that disappears
once a fast provider is picked.

---

## Provider-pool honesty

**Answer quality varies by which model actually served a given response.** Free-tier models differ
significantly in capability, and the gateway's whole design accepts routing a request to whichever one
is available rather than guaranteeing a specific one answers. `provider_used`/`model_used` are logged
per message specifically so this stays visible and debuggable rather than a silent, unexplained quality
swing.

**Rate limits are organization-level for Groq and project-level for Gemini, not per-key.** A second key
on either provider adds nothing — `keys_resolution` (Phase 6) has to treat a user's private key on these
providers as a billing change, not a capacity one, and the gateway does not attempt to work around this
by acquiring additional keys on the same account, which several providers' terms explicitly prohibit
anyway (see below).

**Multi-key farming on a single provider is out of scope, on purpose.** The value this project
demonstrates is combining *independent* providers' free offerings, not generating extra keys or
projects on one provider to inflate a single quota — the latter is against most providers' terms and
answers a different, less interesting engineering question.

**Free-tier data-privacy terms differ by provider, and some may use submitted prompts for model
training.** This matters most for the perception lane (Phase 4), which routes uploaded file content to
a third-party provider for extraction — a real privacy trade-off, not a hypothetical one, and worth a
visible disclosure in the UI once that lane exists rather than a line buried here.

---

## Explicitly out of scope for v1

**Tool-call history across providers.** Incompatible schemas between providers make lossless
translation of tool/function-call history a genuinely unsolved problem in production gateways, not just
this one. D3's answer — the first tool call pins `conversations.pinned_model`, and every later turn in
that conversation ignores slot selection — is a documented limitation rather than a broken translation
attempt. Being able to say *why* this was scoped out, not just that it was, is the stronger position.

**Context-window mismatches are truncated, not summarized.** D4 drops the oldest non-system messages and
inserts a visible omission marker. Summarizing older turns into a compact system message instead is
designed as a seam (`app/memory/summarize.py`) but deliberately not built — it costs quota on every
switch to a smaller-context model and adds a failure mode (a bad summary) that truncation does not have.

**No quota tracking or enforcement yet.** Phase 2's router fails over *reactively*, on a 429 it did not
predict, rather than *proactively* filtering candidates by remaining budget. This is the single largest
scoping line in the phase, stated in `phase2.md` §1 and repeated here because it is also the reason
`auto`'s latency-ranking caveat above exists at all: a quota-blind ranking and a quota-blind failover
loop are the same gap, seen from two directions.

**Not a production system.** Built entirely on free tiers for a portfolio/learning purpose, which means
lower throughput, higher latency variance, and lower consistency than a paid setup would have — stated
plainly here rather than oversold anywhere else in the docs.
