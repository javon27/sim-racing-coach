import json
import sqlite3

import pytest

import scripts.backfill_sectors as bs
from scripts.backfill_sectors import (
    _bounds_lap_count,
    _sector_bounds_for_slug,
    backfill,
    seed_sectors,
)
from simcoach.track_lookup import track_slug

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


# ---- cross-LAYOUT isolation (#41) --------------------------------------------
# The across-cars match is deliberate (sector lines are fixed per track), but it
# must NOT reach across LAYOUTS: seeding Monza GP's splits into the Curva Grande
# map would corrupt a shipped file. It cannot, because a map's filename stem IS
# `track_slug(<full live track name>)` and the layout is part of that name -- the
# only things track_slug collapses are case, accents, punctuation and a trailing
# series suffix (' - ELMS'/' - WEC'), none of which distinguish two layouts.
# These pin that property so a future loosening of track_slug (fuzzy aliases,
# dropping a trailing 'Circuit', ...) can't silently reintroduce the collision.

_MONZA_GP = "Autodromo Nazionale Monza"            # -> autodromo_nazionale_monza
_MONZA_CG = "Monza Curva Grande Circuit"           # -> monza_curva_grande_circuit


def test_monza_layouts_do_not_share_a_slug():
    # The mechanism the isolation rests on, asserted directly.
    assert track_slug(_MONZA_GP) != track_slug(_MONZA_CG)
    assert track_slug(_MONZA_GP) == "autodromo_nazionale_monza"
    assert track_slug(_MONZA_CG) == "monza_curva_grande_circuit"


def test_sector_bounds_never_cross_monza_layouts():
    # Both layouts in one store, each with its own captured splits. Curva Grande
    # is the LATER row, so a name-substring/fuzzy match would hand its bounds to
    # the GP map (the "latest non-null across any car" scan is id DESC).
    conn = _conn_with_laps([
        (1, _MONZA_GP, json.dumps([2000.0, 4000.0])),
        (2, _MONZA_CG, json.dumps([700.0, 1400.0])),
    ])
    assert _sector_bounds_for_slug(conn, "autodromo_nazionale_monza") == [2000.0, 4000.0]
    assert _sector_bounds_for_slug(conn, "monza_curva_grande_circuit") == [700.0, 1400.0]


def test_sector_bounds_for_a_layout_absent_from_the_store_is_none():
    # Only Curva Grande has been driven. The GP map must get NOTHING -- seeding it
    # with the other layout's splits is the Critical corruption #41 asks about.
    conn = _conn_with_laps([(1, _MONZA_CG, json.dumps([700.0, 1400.0]))])
    assert _sector_bounds_for_slug(conn, "autodromo_nazionale_monza") is None


def test_range_validation_alone_cannot_catch_a_cross_layout_mixup():
    # Why the exact-slug match above is the ONLY guard against layout corruption,
    # and seed_sectors' (0, length) check cannot be a second one: the real baked
    # numbers (simcoach/tracks/*.json, 2026-09-09) put each Monza layout's captured
    # splits comfortably inside the OTHER layout's length -- the two lengths differ
    # by 38.5 m (0.7%). Any "does this look plausible for this length?" band wide
    # enough to avoid false rejections would wave this straight through.
    gp_len, cg_len = 5777.59, 5739.08
    gp_bounds, cg_bounds = [1988.5, 3800.0], [1951.4, 3757.4]
    for length in (gp_len, cg_len):
        for bounds in (gp_bounds, cg_bounds):
            d = {"length": length, "sectors": []}
            assert seed_sectors(d, bounds) is True  # in range either way
    assert abs(gp_len - cg_len) / gp_len < 0.01


def test_bounds_lap_count_is_slug_exact():
    # The bake record's "bounds laps=N" must count only the map's OWN layout --
    # an inflated count would misrepresent how well-backed a seeded split is.
    conn = _conn_with_laps([
        (1, _MONZA_GP, json.dumps([1988.5, 3800.0])),
        (2, _MONZA_GP, json.dumps([1988.6, 3800.1])),
        (3, _MONZA_GP, None),                          # no bounds -> not counted
        (4, _MONZA_CG, json.dumps([1951.4, 3757.4])),
    ])
    assert _bounds_lap_count(conn, "autodromo_nazionale_monza") == 2
    assert _bounds_lap_count(conn, "monza_curva_grande_circuit") == 1
    assert _bounds_lap_count(conn, "weathertech_raceway_laguna_seca") == 0


def test_series_suffix_variants_still_share_one_slug():
    # The one merge track_slug performs on purpose: ' - ELMS'/' - WEC' is the same
    # physical track (one map file), so sharing bounds there is correct, not a bug.
    conn = _conn_with_laps([
        (1, "Silverstone Grand Prix Circuit - ELMS", json.dumps([1200.0, 3400.0])),
    ])
    assert _sector_bounds_for_slug(conn, "silverstone_grand_prix_circuit") == [1200.0, 3400.0]
    # ...but a genuinely different Silverstone layout is still isolated.
    assert _sector_bounds_for_slug(conn, "silverstone_national_circuit") is None


# ---- hostile DB rows: arity + type ------------------------------------------
# `laps.sector_bounds` has one writer (smm.sector_bounds_from, which enforces two
# finite values), so these need a hand-edited SQLite file. It's still a trust
# boundary: without the arity and number checks every row below was baked verbatim
# into a shipped map -- a JSON string iterated its characters, a JSON object its
# keys, and `true` floated to a 1 m sector split.

@pytest.mark.parametrize(("raw", "why"), [
    ('[1,2,3,4,5]', "five bounds is not an S1/S2 capture"),
    ('[1988.5]', "one bound is not an S1/S2 capture"),
    ('"1234"', "JSON string -- characters would iterate to [1,2,3,4]"),
    ('{"1000":1,"2000":2}', "JSON object -- keys would iterate to [1000,2000]"),
    ('[true, 3800.0]', "bool -- float(True) would become a 1 m split"),
    ('[null, 3800.0]', "null is not a number"),
    ('["1000", "2000"]', "numeric strings are not numbers"),
    ('[[1000], [2000]]', "nested lists are not numbers"),
])
def test_hostile_sector_bounds_rows_never_reach_a_map(raw, why):
    d = {"length": 5777.59, "sectors": []}
    conn = _conn_with_laps([(1, _MONZA_GP, raw)])
    bounds = _sector_bounds_for_slug(conn, "autodromo_nazionale_monza")
    # Rejected at the DB read, or failing that at seed_sectors -- never written.
    assert not seed_sectors(d, bounds), why
    assert d["sectors"] == [], why


@pytest.mark.parametrize("bad_arity", [[1000.0], [1000.0, 2000.0, 3000.0], []])
def test_seed_sectors_requires_exactly_two_bounds(bad_arity):
    # The check in its own right, for any future caller that doesn't come via
    # _sector_bounds_for_slug. A wrong arity is the tell that the row was never a
    # real S1/S2 capture, so reject rather than bake what's there.
    d = {"length": 5777.59, "sectors": []}
    assert seed_sectors(d, bad_arity) is False
    assert d["sectors"] == []


@pytest.mark.parametrize("bad_length", [
    "5777.59",         # numeric string
    "abc",             # garbage -- must not raise
    [5777.59],         # wrong type -- must not raise
    float("inf"),      # would disable the upper bound entirely
    float("nan"),
    True,              # bool -- float(True) == 1.0
    0, 0.0, -1.0, None,
])
def test_seed_sectors_requires_a_finite_positive_numeric_length(bad_length):
    d = {"length": bad_length, "sectors": []}
    assert seed_sectors(d, [1988.5, 3800.0]) is False  # returns, never raises
    assert d["sectors"] == []


# ---- backfill(): write safety + run robustness -------------------------------

def _fake_tracks(tmp_path, maps):
    """maps: {slug: dict}. Writes them as TRACKS_DIR would hold them."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    for slug, d in maps.items():
        (tmp_path / f"{slug}.json").write_text(
            json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _fake_db(tmp_path, rows):
    db = tmp_path / "laps.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE laps (id INTEGER PRIMARY KEY, track TEXT, sector_bounds TEXT)")
    conn.executemany("INSERT INTO laps (id, track, sector_bounds) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def _wire(monkeypatch, tracks_dir, db):
    monkeypatch.setattr(bs, "TRACKS_DIR", tracks_dir)
    monkeypatch.setattr(bs, "db_path", lambda: db)


def test_backfill_write_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    # A plain open('w') truncates before any bytes land, so an abort mid-write
    # leaves a shipped map truncated (demonstrated at 138548 -> 69145 bytes). With
    # tmp+os.replace, a failed rename must leave the ORIGINAL byte-identical.
    tracks = _fake_tracks(tmp_path / "tracks", {
        "autodromo_nazionale_monza": {"length": 5777.59, "sectors": [], "corners": [{"radius_m": 67.7}]},
    })
    before = (tracks / "autodromo_nazionale_monza.json").read_bytes()
    _wire(monkeypatch, tracks, _fake_db(tmp_path, [(1, _MONZA_GP, json.dumps([1988.5, 3800.0]))]))
    monkeypatch.setattr(bs.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    with pytest.raises(OSError, match="boom"):
        backfill()

    assert (tracks / "autodromo_nazionale_monza.json").read_bytes() == before  # not truncated
    assert list(tracks.glob("*.tmp")) == []  # no debris left behind


def test_backfill_survives_a_bad_length_and_still_seeds_the_rest(tmp_path, monkeypatch, capsys):
    # The behaviour that matters: one broken map must not strand the maps after it.
    # Previously `float("abc")` aborted the run mid-way, and the partial state was
    # sticky -- a re-run hit the same map and died in the same place.
    tracks = _fake_tracks(tmp_path / "tracks", {
        "autodromo_nazionale_monza": {"length": "abc", "sectors": []},       # sorts first
        "monza_curva_grande_circuit": {"length": 5739.08, "sectors": []},    # must still be reached
    })
    _wire(monkeypatch, tracks, _fake_db(tmp_path, [
        (1, _MONZA_GP, json.dumps([1988.5, 3800.0])),
        (2, _MONZA_CG, json.dumps([1951.4, 3757.4])),
    ]))

    assert backfill() == 2  # ran to completion over both files

    gp = json.loads((tracks / "autodromo_nazionale_monza.json").read_text(encoding="utf-8"))
    cg = json.loads((tracks / "monza_curva_grande_circuit.json").read_text(encoding="utf-8"))
    assert gp["sectors"] == []                  # skipped, not written
    assert gp["length"] == "abc"                # and not "repaired"
    assert cg["sectors"] == [1951.4, 3757.4]    # the rest still got seeded
    assert "is not a finite positive number" in capsys.readouterr().out


def test_backfill_is_repo_relative_and_reports_an_empty_run(tmp_path, monkeypatch):
    # A cwd-relative TRACKS_DIR made a run from the wrong directory exit 0 with empty
    # stdout -- "already baked" and "pointed at the wrong place" looked identical.
    assert bs.TRACKS_DIR.is_absolute()
    assert (bs.TRACKS_DIR / "autodromo_nazionale_monza.json").exists()
    _wire(monkeypatch, _fake_tracks(tmp_path / "tracks", {}), _fake_db(tmp_path, []))
    assert backfill() == 0  # caller (__main__) turns 0 into a non-zero exit


def test_unusable_newest_row_warns_before_falling_back(capsys):
    # Falling back to an older lap is right; doing it silently is not -- the bake
    # record would claim the latest capture when it used an older one.
    conn = _conn_with_laps([
        (1, _MONZA_GP, json.dumps([1988.5, 3800.0])),   # older, good
        (2, _MONZA_GP, '"1234"'),                       # newest, unusable
    ])
    assert _sector_bounds_for_slug(conn, "autodromo_nazionale_monza") == [1988.5, 3800.0]
    out = capsys.readouterr().out
    assert "warning" in out and "lap 2" in out
