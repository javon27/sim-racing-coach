"""Resolve the game's live vehicle name to a car MODEL for the coach.

LMU shared memory / REST expose only the *class* ('GT3') and the *team/entry* name
(e.g. 'Iron Dames 2025 #85:ELMS', 'Racing Team Turkey by TF 2026 #34:WEC') — NOT the
specific car model. So model resolution is an alias table keyed on the entry name,
which the user maintains (team<->car changes by season). Unknown entries fall back to
the class, so the coach still runs (just without a model-specific note)."""
from __future__ import annotations

# entry-name substring (lowercased) -> car model display name.
# The display name slugifies (in coach.build_prompt) to the knowledge-note filename,
# e.g. 'Porsche 911 GT3 R' -> porsche_911_gt3_r.md. EDIT THIS as you race new entries.
CAR_ALIASES: dict[str, str] = {
    "iron dames": "Porsche 911 GT3 R",              # user confirmed racing the Porsche
    "by tf": "Chevrolet Corvette Z06 GT3.R",        # TF Sport run the Corvette in WEC LMGT3
    "racing team turkey": "Chevrolet Corvette Z06 GT3.R",
}


def resolve_car(vehicle_name: str, car_class: str | None = None) -> str:
    """Return a car MODEL display name for a live vehicle/entry name.

    Matches the entry name against CAR_ALIASES; falls back to '<class> car' (e.g.
    'GT3 car') when unknown, or the raw entry name if the class is also unknown.
    """
    name = (vehicle_name or "").lower()
    for substr, model in CAR_ALIASES.items():
        if substr in name:
            return model
    if car_class:
        return f"{car_class} car"
    return vehicle_name or "unknown car"
