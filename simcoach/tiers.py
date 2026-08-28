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
