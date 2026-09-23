"""JSON wire shaping for the sidecar: non-finite floats to null, chat usage view. Moved from app.py (#113)."""
from __future__ import annotations

import math

from ..pricing import estimate_chat_cost


def _usage_view(agg: dict) -> dict:
    """Shape a LapStore.chat_usage() aggregate for the wire: the four buckets +
    total + message count, the estimated $, and whether that $ is the whole story.

    The core owns the arithmetic (pricing lives in simcoach.pricing) — the UI only
    formats what it's given. `by_model` stays server-side; the client has no use for
    the breakdown, only for the one number and the completeness flag.

    A chat with any pre-v12/NULL-usage row (`null_usage_rows`) is never "complete" —
    those rows billed real money that COALESCEs to 0, so a chat that is ALL such rows
    must not read as a genuinely-zero-cost, fully-priced chat."""
    usage = {k: agg.get(k, 0) for k in
             ("input", "output", "cache_read", "cache_write", "total", "messages")}
    cost, complete = estimate_chat_cost(agg.get("by_model") or {})
    if agg.get("null_usage_rows"):
        complete = False
    return {"usage": usage, "cost": cost, "cost_complete": complete}


def _clean(v):
    # CornerMetrics.min_spd et al. can be float('nan') on real data;
    # NaN/Inf are invalid JSON, so map non-finite floats to None.
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v
