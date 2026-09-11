# tests/test_backfill_corner_geometry.py
#
# The bake itself only runs on the curator's machine -- it reads the app-data captures and
# the lap store -- so what its OUTPUT should be is asserted against the committed maps
# instead, in tests/test_map_save_geometry.py
# (test_every_shipped_radius_is_measured_on_its_own_corner_window). What is testable off
# that machine is the script's refusal to run a bake it cannot do exactly as typed.
#
# Every test here runs inside `sandbox`, and that is load-bearing, not tidiness: with a
# check reverted, the script falls straight through into the REAL bake, which reads the
# curator's app-data and rewrites simcoach/tracks/*.json. The sandbox points the tracks
# dir at a throwaway copy and makes opening app-data an immediate failure, so a broken
# check shows up as a red test instead of as a silently rewritten shipped map.
import shutil
from pathlib import Path

import pytest

import scripts.backfill_corner_geometry as bf
import simcoach
from simcoach.analysis import CORNER_GEOMETRY_KEYS

SHIPPED = Path(simcoach.__file__).resolve().parent / "tracks"
IMOLA = "autodromo_enzo_e_dino_ferrari"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    tracks = tmp_path / "tracks"
    shutil.copytree(SHIPPED, tracks)
    monkeypatch.setattr(bf, "TRACKS_DIR", tracks)

    def opened(*_a, **_k):
        raise AssertionError("opened app-data: the invocation was not refused up front")

    monkeypatch.setattr(bf, "telemetry_dir", opened)
    monkeypatch.setattr(bf, "db_path", opened)
    return tracks


@pytest.mark.parametrize(
    "argv,named",
    [(["--onyl=radius_m"], "--onyl=radius_m"),   # typo: used to bake EVERY metric
     (["--only="], "--only="),                    # empty: same fallback
     (["--only=radius_m,"], "--only=radius_m,"),  # an empty name inside the list
     (["--only", "radius_m"], "--only"),          # space form: radius_m read as a slug
     (["--only=elevation"], "['elevation']"),     # a metric that does not exist
     (["--only=radius_m", "imola"], "['imola']")],  # a slug that matches no map
)
def test_a_bad_invocation_exits_nonzero_before_opening_anything(sandbox, argv, named):
    """Each of these used to run to completion and print a normal-looking report, so the
    curator committed a re-bake that either did not happen or rewrote metrics nobody
    asked for. Through `main`, the real argv entry point -- not `backfill()`, which never
    sees the argv parse that the first four cases break.

    Non-zero: a SystemExit carrying a message exits with status 1. Before anything is
    opened: `sandbox` fails the test on the first app-data access. The message must name
    the offending token, so the curator can see WHICH argument was refused."""
    with pytest.raises(SystemExit) as e:
        bf.main(argv)
    assert e.value.code not in (0, None), "exited as success"
    assert named in str(e.value.code)


@pytest.mark.parametrize(
    "argv,slugs,keys",
    [([], None, CORNER_GEOMETRY_KEYS),
     (["--only=radius_m"], None, ("radius_m",)),
     ([IMOLA, "--only=radius_m,elev_delta_m"], {IMOLA}, ("radius_m", "elev_delta_m"))],
)
def test_a_good_invocation_reaches_the_bake_with_exactly_what_was_typed(
        sandbox, monkeypatch, argv, slugs, keys):
    """The other half of the refusals above: without it they are satisfied by a `main`
    that refuses everything."""
    calls = []
    monkeypatch.setattr(bf, "backfill", lambda s, k: calls.append((s, k)))
    bf.main(argv)
    assert calls == [(slugs, keys)]


@pytest.mark.parametrize(
    "kwargs,named",
    [({"keys": ("radius_m", "elevation")}, "not corner geometry metrics: ['elevation']"),
     ({"keys": ()}, "no metrics to bake"),
     ({"slugs": {"imola"}}, "no shipped map named: ['imola']")],
)
def test_backfill_itself_refuses_the_same_invocations(sandbox, kwargs, named):
    """`backfill` is importable, so it holds the same checks (one owner, `_usage_error`)
    rather than trusting that every caller came through `main`. The metric message must
    list ONLY the unknown name -- a check that rejected everything would list radius_m
    too."""
    with pytest.raises(ValueError) as e:
        bf.backfill(**kwargs)
    assert named in str(e.value)


def test_the_default_bake_is_every_metric():
    """No `--only` must stay "bake everything", and the metric list must stay the shared
    one -- a private copy here is the drift shape that deleted Laguna's width/banking
    (#62)."""
    assert bf._METRIC_KEYS is CORNER_GEOMETRY_KEYS
    assert bf.backfill.__defaults__ == (None, CORNER_GEOMETRY_KEYS)
