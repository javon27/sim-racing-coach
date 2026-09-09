import json
import sqlite3

import pytest

from scripts.backfill_sectors import _sector_bounds_for_slug, seed_sectors

# ---- seed_sectors -----------------------------------------------------------

def test_seed_sectors_sets_sectors_when_empty():
    d = {"length": 3600.0, "sectors": []}
    assert seed_sectors(d, [1395.0, 2840.0]) is True
    assert d["sectors"] == [1395.0, 2840.0]


def test_seed_sectors_rounds_to_one_decimal():
    d = {"length": 3600.0, "sectors": []}
    seed_sectors(d, [1395.04, 2840.06])
    assert d["sectors"] == [1395.0, 2840.1]


def test_seed_sectors_leaves_already_seeded_map_untouched():
    d = {"length": 3600.0, "sectors": [1000.0, 2000.0]}
    assert seed_sectors(d, [1395.0, 2840.0]) is False
    assert d["sectors"] == [1000.0, 2000.0]  # not overwritten


def test_seed_sectors_noop_without_captured_bounds():
    d = {"length": 3600.0, "sectors": []}
    assert seed_sectors(d, None) is False
    assert seed_sectors(d, []) is False
    assert d["sectors"] == []


def test_seed_sectors_noop_without_length():
    d = {"sectors": []}
    assert seed_sectors(d, [1395.0, 2840.0]) is False
    assert "sectors" not in d or d["sectors"] == []


@pytest.mark.parametrize("bad_bounds", [
    [0.0, 2840.0],        # not > 0
    [1395.0, 3600.0],     # not < length
    [1395.0, float("nan")],
    [1395.0, float("inf")],
    [2840.0, 1395.0],     # not strictly increasing
    [1395.0, 1395.0],     # not strictly increasing (equal)
])
def test_seed_sectors_rejects_invalid_bounds(bad_bounds):
    d = {"length": 3600.0, "sectors": []}
    assert seed_sectors(d, bad_bounds) is False
    assert d["sectors"] == []


# ---- _sector_bounds_for_slug -------------------------------------------------

def _conn_with_laps(rows):
    """rows: list of (id, track, sector_bounds_json_or_None)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE laps (id INTEGER PRIMARY KEY, track TEXT, sector_bounds TEXT)")
    conn.executemany("INSERT INTO laps (id, track, sector_bounds) VALUES (?, ?, ?)", rows)
    conn.commit()
    return conn


def test_sector_bounds_for_slug_matches_across_cars():
    # Same track slug, different cars (live track names differ by series suffix
    # but share a slug) -- any car's captured bounds are the track's.
    conn = _conn_with_laps([
        (1, "WeatherTech Raceway Laguna Seca", json.dumps([1395.1, 2840.2])),
        (2, "WeatherTech Raceway Laguna Seca", None),
    ])
    assert _sector_bounds_for_slug(conn, "weathertech_raceway_laguna_seca") == [1395.1, 2840.2]


def test_sector_bounds_for_slug_picks_latest_non_null():
    conn = _conn_with_laps([
        (1, "Autodromo Nazionale Monza", json.dumps([999.0, 1999.0])),   # older
        (2, "Autodromo Nazionale Monza", None),                          # no bounds captured
        (3, "Autodromo Nazionale Monza", json.dumps([1000.0, 2000.0])),  # latest
    ])
    assert _sector_bounds_for_slug(conn, "autodromo_nazionale_monza") == [1000.0, 2000.0]


def test_sector_bounds_for_slug_ignores_other_tracks():
    conn = _conn_with_laps([
        (1, "Autodromo Nazionale Monza", json.dumps([1000.0, 2000.0])),
    ])
    assert _sector_bounds_for_slug(conn, "weathertech_raceway_laguna_seca") is None


def test_sector_bounds_for_slug_none_when_no_lap_has_bounds():
    conn = _conn_with_laps([
        (1, "Autodromo Nazionale Monza", None),
    ])
    assert _sector_bounds_for_slug(conn, "autodromo_nazionale_monza") is None


def test_sector_bounds_for_slug_skips_unparseable_json():
    conn = _conn_with_laps([
        (1, "Autodromo Nazionale Monza", "not json"),
        (2, "Autodromo Nazionale Monza", None),
    ])
    assert _sector_bounds_for_slug(conn, "autodromo_nazionale_monza") is None
