"""Claude API prices and the cost estimate built from them.

This module is the ONE place a dollar-per-token figure appears. Nothing else —
not the server, not the frontend — may hold a rate; they receive a computed
number. `ponytail:` prices drift, so keep the table here and update it here.

    Confirmed 2026-09-09 against the live pricing page:
    https://platform.claude.com/docs/en/about-claude/pricing
    Every rate below (input, output, cache read, cache write) matches that
    page for all three models. Pricing can still change — re-check the live
    page before a release and update this date if anything moved.

    Note for future readers: Claude Sonnet 5's $2/$10 was originally
    announced as *introductory* pricing due to rise to $3/$15 on 2026-09-01.
    The live docs now state that increase will NOT occur — $2/$10 is the
    standard price. Do not "correct" it back to $3/$15 from a stale memory.

    cache_write here is the 5-minute ephemeral TTL rate (1.25x input), which
    is what every current caller means by "cache write". Issue #42 landed
    prompt caching on `{"type": "ephemeral"}` — the DEFAULT 5-minute TTL — so
    this column is the right rate for it. The 1-hour TTL prices at 2x input,
    not 1.25x: if anything ever passes `ttl: "1h"`, price it at 2x rather
    than reusing this column, or the meter under-reports those writes by 37.5%.

Unknown model -> None, never 0.0: a fabricated $0 is a lie a BYO-key driver
would act on. A genuinely free provider (the fake model, a future local model)
returns 0.0 — that is an arithmetic fact, and it is the UI's job to render it
as "—" rather than "$0.00" so the meter never implies a paid thing was free.
"""
from __future__ import annotations

# model_id: ($ per MILLION tokens) input, output, cache_read, cache_write
# cache_write is the 5-minute-TTL rate (1.25x input), which is the TTL the coach
# loop uses. The 1-hour-TTL rate is 2x input, not represented here — see docstring.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5":    (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5":  (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00,  5.00, 0.10, 1.25),
}

# Providers that cost nothing in API dollars. Not in PRICES — they have no rates.
FREE = ("fake", "local")

# The TokenUsage bucket order PRICES tuples are indexed in.
_BUCKETS = ("input", "output", "cache_read", "cache_write")


def _rates(model: str | None) -> tuple[float, float, float, float] | None:
    """Resolve a model id to its rates, or None if it can't be priced.

    Exact hit first, then the longest PRICES key that is a PREFIX of the id — which
    is how a dated snapshot (`claude-haiku-4-5-20251001`, the id factory.py's haiku
    alias resolves to) prices as its base model. Deliberately one-directional: an id
    that is merely a prefix of a table key (a truncated or garbled value) does NOT
    match, so a bad id reads as unpriceable instead of silently costing like a real
    model."""
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    if model in FREE:
        return (0.0, 0.0, 0.0, 0.0)
    matches = [k for k in PRICES if model.startswith(k)]
    if matches:
        return PRICES[max(matches, key=len)]
    return None


def estimate_cost(usage, model: str | None) -> float | None:
    """Estimated $ for one usage record on `model`, or None if unpriceable.

    `usage` is a TokenUsage or a plain dict of its buckets (missing keys = 0)."""
    rates = _rates(model)
    if rates is None:
        return None
    tokens = usage.as_dict() if hasattr(usage, "as_dict") else usage
    return sum((tokens.get(b) or 0) / 1e6 * rate for b, rate in zip(_BUCKETS, rates))


def estimate_chat_cost(by_model: dict[str, dict]) -> tuple[float | None, bool]:
    """Total estimated $ for a whole chat, plus whether that total is complete.

    Takes LapStore.chat_usage()'s `by_model` breakdown, because a chat whose model
    changed mid-way is only priceable row by row. `complete` is False when at least
    one model that actually billed tokens could not be priced — the UI then shows
    "≥ $x" instead of passing a known-short total off as the real number. An
    unpriceable model that billed nothing (e.g. a legacy pre-v12 row, keyed "")
    costs nothing and leaves the total complete."""
    total = 0.0
    complete = True
    for model, tokens in by_model.items():
        cost = estimate_cost(tokens, model)
        if cost is None:
            if any((tokens.get(b) or 0) for b in _BUCKETS):
                complete = False
            continue
        total += cost
    return total, complete
