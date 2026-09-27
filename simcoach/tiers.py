from __future__ import annotations

from enum import Enum


class Tier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


# Human-readable adjustable items per tier. None = fully open (no constraint).
# Bronze is authoritative-known. SILVER is an EDUCATED GUESS (flag for review).
TIER_ADJUSTABLE: dict[Tier, list[str] | None] = {
    Tier.BRONZE: ["traction control", "brake bias", "ABS"],
    Tier.SILVER: ["traction control", "brake bias", "ABS",
                  "tire pressures", "anti-roll bars", "differential", "downforce/wing"],
    Tier.GOLD: None,
}


def tier_adjustable(tier: Tier) -> list[str] | None:
    return TIER_ADJUSTABLE[tier]


def adjustable_from_setup(setup: dict) -> list[str]:
    """Ground-truth path: given LMU getPlayerGarageData (key -> {isFreeSetting,...}),
    return the keys whose isFreeSetting is True. This is what the live app will
    use; the TIER_ADJUSTABLE map is the offline/explainer fallback."""
    return [key for key, val in setup.items()
            if isinstance(val, dict) and val.get("isFreeSetting") is True]


def blank_setup(setup: dict) -> bool:
    """True when no entry has a non-blank stringValue: a garage payload with nothing
    loaded (#188 -- a real one had 170 isFreeSetting keys, every stringValue ''). Such
    a payload passes adjustable_from_setup but is not a setup, so it is never stored."""
    return not any(isinstance(v, dict) and str(v.get("stringValue") or "").strip()
                   for v in setup.values())


def legal_adjustments(setup: dict) -> list[str] | None:
    """Tier-legal setup adjustments as prettified labels, for the coach's
    allowed_adjustments constraint. Returns None when the session is UNRESTRICTED
    (every param adjustable) — an open setup needs no constraint, and passing all
    ~170 keys would just bloat the prompt."""
    params = [k for k, v in setup.items() if isinstance(v, dict) and "stringValue" in v]
    free = adjustable_from_setup(setup)
    if not free or len(free) >= len(params):
        return None
    return [(k.removeprefix("VM_")).replace("_", " ").title() for k in free]
