"""Resolver maps the game's live track names (exact strings observed from LMU shared
memory) to our corner-map files."""
import pytest
from simcoach.track_lookup import resolve_track_map

CASES = [
    ("Silverstone Grand Prix Circuit - ELMS", "silverstone"),
    ("Autodromo Jose Carlos Pace", "autodromo_jos_carlos_pace"),  # accents stripped
    ("Algarve International Circuit", "algarve_international_circuit"),
    ("Autodromo Enzo e Dino Ferrari", "autodromo_enzo_e_dino_ferrari"),
    ("Autodromo Nazionale Monza", "autodromo_nazionale_monza"),
    ("Monza Curva Grande Circuit", "monza_curva_grande_circuit"),
    ("Bahrain International Circuit", "bahrain_international_circuit"),
    ("Bahrain Outer Circuit", "bahrain_outer_circuit"),
    ("Bahrain Paddock Circuit", "bahrain_paddock_circuit"),
    ("Circuit de Barcelona", "circuit_de_barcelona"),
    ("Circuit de la Sarthe", "lemans"),  # alias: game name shares no token with slug
    ("Circuit de la Sarthe Mulsanne", "circuit_de_la_sarthe_mulsanne"),  # no-chicane variant
]


@pytest.mark.parametrize("live,expected", CASES)
def test_resolve_track_map(live, expected):
    p = resolve_track_map(live, "simcoach/tracks")
    assert p is not None, f"no map resolved for {live!r}"
    assert p.stem == expected, f"{live!r} -> {p.stem}, expected {expected}"


def test_unknown_track_returns_none():
    assert resolve_track_map("Nonexistent Raceway ZZZ", "simcoach/tracks") is None
