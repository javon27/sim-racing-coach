"""Track resolution: a live track name maps to a corner-map file by a deterministic
slug of the (series-suffix-stripped, accent-normalized) full name — distinct layouts
get distinct files, and an unknown layout resolves to None so the caller auto-builds."""
import pytest

from simcoach.track_lookup import resolve_track_map, track_slug

SLUG_CASES = [
    ("Silverstone Grand Prix Circuit - ELMS", "silverstone_grand_prix_circuit"),  # series suffix dropped
    ("Silverstone National Circuit", "silverstone_national_circuit"),             # distinct from GP
    ("Circuit de la Sarthe", "circuit_de_la_sarthe"),
    ("Circuit de la Sarthe Mulsanne", "circuit_de_la_sarthe_mulsanne"),           # distinct layout
    ("Autódromo José Carlos Pace", "autodromo_jose_carlos_pace"),                 # accents normalized
    ("Circuit de Spa-Francorchamps", "circuit_de_spa_francorchamps"),             # inner hyphen kept
    ("Autodromo Nazionale Monza - WEC", "autodromo_nazionale_monza"),
]


@pytest.mark.parametrize("live,expected", SLUG_CASES)
def test_track_slug(live, expected):
    assert track_slug(live) == expected


def test_gp_and_national_resolve_to_different_maps():
    gp = resolve_track_map("Silverstone Grand Prix Circuit - ELMS", "simcoach/tracks")
    nat = resolve_track_map("Silverstone National Circuit", "simcoach/tracks")
    assert gp is not None and gp.stem == "silverstone_grand_prix_circuit"
    assert nat is not None and nat.stem == "silverstone_national_circuit"
    assert gp != nat


def test_series_suffix_shares_one_map():
    # ELMS vs WEC of the same track -> same slug -> same map file
    assert (track_slug("Silverstone Grand Prix Circuit - ELMS")
            == track_slug("Silverstone Grand Prix Circuit - WEC"))


def test_unknown_layout_returns_none(tmp_path):
    assert resolve_track_map("Nonexistent Raceway ZZZ", str(tmp_path)) is None
