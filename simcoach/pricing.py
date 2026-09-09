"""Claude API prices and the cost estimate built from them.

This module is the ONE place a dollar-per-token figure appears. Nothing else —
not the server, not the frontend — may hold a rate; they receive a computed
number. `ponytail:` prices drift, so keep the table here and update it here.

    ⚠  RE-CONFIRM THESE RATES BEFORE RELEASE.
    The input/output figures come from the bundled `claude-api` skill's model
    table, cached 2026-06-24, and the cache columns from its documented
    multipliers (cache read ~0.1x input, cache write ~1.25x input for the
    default 5-minute ephemeral TTL). Re-read that skill (or Anthropic's live
    pricing page) at release time, fix any row that moved, and update the
    confirmation date below. Showing a BYO-key driver a WRONG dollar figure is
    worse than showing none.

    Last confirmed: 2026-09-08 against the bundled `claude-api` skill table
    (itself cached 2026-06-24). NOT yet confirmed against live Anthropic
    pricing — that is a release gate, and the numbers the driver sees are only
    as current as this line.

Unknown model -> None, never 0.0: a fabricated $0 is a lie a BYO-key driver
would act on. A genuinely free provider (the fake model, a future local model)
returns 0.0 — that is an arithmetic fact, and it is the UI's job to render it
as "—" rather than "$0.00" so the meter never implies a paid thing was free.
"""
from __future__ import annotations

# model_id: ($ per MILLION tokens) input, output, cache_read, cache_write
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
