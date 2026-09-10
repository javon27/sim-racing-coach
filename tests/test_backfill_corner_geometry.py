# tests/test_backfill_corner_geometry.py
#
# The bake itself only runs on the curator's machine -- it reads the app-data captures and
# the lap store -- so what its OUTPUT should be is asserted against the committed maps
# instead, in tests/test_map_save_geometry.py
# (test_every_shipped_radius_is_measured_on_its_own_corner_window). What is testable off
# that machine is the script's one piece of argument logic.
import pytest

from scripts.backfill_corner_geometry import _METRIC_KEYS, backfill


def test_baking_an_unknown_metric_raises_instead_of_baking_nothing():
    """`--only=` exists so a re-bake forced by ONE metric's source changing can ship a
    diff a reviewer can read (#73: 18 radius_m values, not those plus 59 unrelated
    elev_delta_m ones). Its failure mode is SILENT: a mistyped metric matches nothing, so
    the script prints `baked N corners | metrics=[]`, writes every file back unchanged,
    and the curator commits a re-bake that did not happen.

    The rejection must also NAME only the bad metric -- a check that rejected everything
    would satisfy `raises` just as well, and there is no cheap positive case to pair it
    with (a valid key runs the real bake against app-data). And it has to raise before
    any of that: `backfill` opens the lap store on the next line, so this test only runs
    off the curator's machine because the check comes first."""
    with pytest.raises(ValueError) as e:
        backfill(keys=("radius_m", "elevation"))
    assert "elevation" in str(e.value)
    assert "radius_m" not in str(e.value), "rejects valid metrics too"


def test_the_default_bake_is_every_metric():
    """No `--only` must stay "bake everything", and the metric list must stay the shared
    one -- a private copy here is the drift shape that deleted Laguna's width/banking
    (#62)."""
    from simcoach.analysis import CORNER_GEOMETRY_KEYS

    assert _METRIC_KEYS is CORNER_GEOMETRY_KEYS
    assert backfill.__defaults__ == (None, CORNER_GEOMETRY_KEYS)
