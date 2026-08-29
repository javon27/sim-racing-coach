"""Resolve the game's live track name (e.g. 'Silverstone Grand Prix Circuit - ELMS',
'Autodromo Jose Carlos Pace', 'Circuit de la Sarthe') to one of our corner-map files
(silverstone.json, autodromo_jos_carlos_pace.json, lemans.json). The game name and our
filename diverge (suffixes, accents, our own slug choices), so match by shared
meaningful tokens rather than exact slug, with an alias table for names that share none."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Extra match tokens for maps whose file slug shares no token with the game's
# track name (e.g. the game calls Le Mans 'Circuit de la Sarthe').
ALIASES: dict[str, set[str]] = {
    "lemans": {"sarthe", "mans", "le"},
    "spa": {"francorchamps"},
}
# Generic words / connectors that shouldn't drive a match (a bare "de" must not
# tie 'Circuit de la Sarthe' to 'circuit_de_barcelona').
_NOISE = {"circuit", "the", "of", "gp", "de", "la", "le", "di", "del", "della"}

# Explicit layout disambiguation for multi-config circuits: if the live name contains
# ALL of `required`, use `slug`. Checked before the generic scorer, and these variant
# slugs are excluded from it — so 'Circuit de la Sarthe Mulsanne' picks the no-chicane
# map while plain 'Circuit de la Sarthe' still resolves to full-chicane 'lemans'.
LAYOUT_RULES: list[tuple[frozenset[str], str]] = [
    (frozenset({"mulsanne"}), "circuit_de_la_sarthe_mulsanne"),
]


def _tokens(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t and t not in _NOISE}


def resolve_track_map(track_name: str, tracks_dir: str = "simcoach/tracks") -> Path | None:
    """Return the best-matching corner-map path for a live track name, or None.

    Scores each map by (# shared meaningful tokens, fraction of the map's tokens
    matched) so a layout-specific map (bahrain_outer) beats the base one only when
    the distinguishing token is present, and a short slug (silverstone) still wins
    when it's the only shared token.
    """
    live = _tokens(track_name)
    for required, slug in LAYOUT_RULES:
        if required <= live:
            p = Path(tracks_dir) / f"{slug}.json"
            if p.exists():
                return p
    variant_slugs = {slug for _, slug in LAYOUT_RULES}
    best: Path | None = None
    best_key = (0, 0.0)
    for p in sorted(Path(tracks_dir).glob("*.json")):
        if p.stem == "test_track" or p.stem in variant_slugs:
            continue
        mtok = _tokens(p.stem) | ALIASES.get(p.stem, set())
        inter = live & mtok
        if not inter:
            continue
        key = (len(inter), len(inter) / len(mtok))
        if key > best_key:
            best_key, best = key, p
    return best
