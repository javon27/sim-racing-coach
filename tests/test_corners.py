import glob
import itertools
import json

import numpy as np
import pytest

from simcoach.model_types import LapRecord


def test_computed_direction_matches_geometry(synthetic_lap):
    from simcoach.corners import compute_direction
    assert compute_direction(synthetic_lap, 800, 1200) == "left"
    assert compute_direction(synthetic_lap, 2000, 2400) == "right"


def test_map_directions_consistent(synthetic_lap):
    from simcoach.corners import CornerMap, verify_map_directions
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    assert verify_map_directions(synthetic_lap, cmap) == []


def test_verify_map_directions_detects_wrong_direction(synthetic_lap):
    """Detection test: verify_map_directions catches a flipped direction in the map."""
    from simcoach.corners import Corner, CornerMap, verify_map_directions

    # Load correct map, mutate one corner's direction
    cmap = CornerMap.load("simcoach/tracks/test_track.json")
    # Flip corner 1 "Left Hairpin" to "right" (wrong)
    cmap.corners[0] = Corner(
        number=1, name="Left Hairpin", direction="right",
        start_dist=800, apex_dist=1000, end_dist=1200
    )

    # Should detect mismatch: declared "right", computed "left"
    mismatches = verify_map_directions(synthetic_lap, cmap)
    assert (1, "right", "left") in mismatches


def test_compute_direction_raises_on_empty_window(synthetic_lap):
    """Empty window (no telemetry in range) raises ValueError."""
    from simcoach.corners import compute_direction

    # Window entirely outside lap range (0-3000)
    with pytest.raises(ValueError, match="no telemetry in window"):
        compute_direction(synthetic_lap, 3100, 3200)


def test_compute_direction_raises_on_all_nan_window(synthetic_lap):
    """All-NaN window raises ValueError."""
    from simcoach.corners import compute_direction

    # Create lap with steering all NaN in a specific range
    n = 1000
    dist = np.linspace(0, 3000, n)
    channels = dict(synthetic_lap.channels)
    steering = channels["steering"].copy().astype(float)
    # Set steering to NaN in a region
    nan_mask = (dist > 800) & (dist < 1200)
    steering[nan_mask] = np.nan
    channels["steering"] = steering

    lap_with_nan = LapRecord(
        sim=synthetic_lap.sim,
        track=synthetic_lap.track,
        car=synthetic_lap.car,
        lap_time=synthetic_lap.lap_time,
        valid=synthetic_lap.valid,
        timestamp=synthetic_lap.timestamp,
        channels=channels,
    )

    # Query the all-NaN region raises ValueError
    with pytest.raises(ValueError, match="no telemetry in window"):
        compute_direction(lap_with_nan, 800, 1200)


def test_compute_direction_tolerates_partial_nan_via_nanmean(synthetic_lap):
    """Partial NaN samples are skipped; nanmean computes from remaining valid samples."""
    from simcoach.corners import compute_direction

    # Create lap with steering partially NaN (but steering sign still clear in valid samples)
    n = 1000
    dist = np.linspace(0, 3000, n)
    channels = dict(synthetic_lap.channels)
    steering = channels["steering"].copy().astype(float)
    # Set half the samples in the left corner region to NaN
    nan_mask = (dist > 800) & (dist < 1000)
    steering[nan_mask] = np.nan
    channels["steering"] = steering

    lap_with_partial_nan = LapRecord(
        sim=synthetic_lap.sim,
        track=synthetic_lap.track,
        car=synthetic_lap.car,
        lap_time=synthetic_lap.lap_time,
        valid=synthetic_lap.valid,
        timestamp=synthetic_lap.timestamp,
        channels=channels,
    )

    # Should still compute "left" from the remaining negative steering samples
    result = compute_direction(lap_with_partial_nan, 800, 1200)
    assert result == "left"


def test_corner_at_returns_correct_corner():
    """corner_at returns the corner containing a given distance."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    # Inside corner 1 (800-1200)
    corner = cmap.corner_at(1000)
    assert corner is not None
    assert corner.number == 1
    assert corner.name == "Left Hairpin"

    # Inside corner 2 (2000-2400)
    corner = cmap.corner_at(2200)
    assert corner is not None
    assert corner.number == 2
    assert corner.name == "Right Sweeper"


def test_corner_at_inclusive_boundaries():
    """corner_at includes start and end boundaries."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    # Start boundary of corner 1
    corner = cmap.corner_at(800)
    assert corner is not None
    assert corner.number == 1

    # End boundary of corner 1
    corner = cmap.corner_at(1200)
    assert corner is not None
    assert corner.number == 1


def test_corner_at_returns_none_for_gap():
    """corner_at returns None when distance falls in a gap between corners."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/test_track.json")

    # Gap between corner 1 (ends 1200) and corner 2 (starts 2000)
    corner = cmap.corner_at(1500)
    assert corner is None


def _write_track_json(tmp_path, corners):
    path = tmp_path / "bad_track.json"
    path.write_text(json.dumps({"track": "bad_track", "corners": corners}))
    return str(path)


def test_load_rejects_apex_outside_window(tmp_path):
    """CornerMap.load raises ValueError when apex_dist is outside [start_dist, end_dist]."""
    from simcoach.corners import CornerMap

    corners = [
        {"number": 1, "name": "Bad Apex", "direction": "left",
         "start_dist": 800, "apex_dist": 1300, "end_dist": 1200},
    ]
    path = _write_track_json(tmp_path, corners)

    with pytest.raises(ValueError, match="apex"):
        CornerMap.load(path)


def test_load_accepts_wrap_rejects_degenerate(tmp_path):
    """start_dist > end_dist is a valid S/F-seam WRAP; only start == end is degenerate."""
    from simcoach.corners import CornerMap

    wrap = _write_track_json(tmp_path, [
        {"number": 1, "name": "Seam", "direction": "left",
         "start_dist": 1200, "apex_dist": 1300, "end_dist": 800},  # wraps; apex >= start
    ])
    assert CornerMap.load(wrap).corners[0].start_dist == 1200  # loads fine

    degen = _write_track_json(tmp_path, [
        {"number": 1, "name": "Degen", "direction": "left",
         "start_dist": 900, "apex_dist": 900, "end_dist": 900},
    ])
    with pytest.raises(ValueError, match="degenerate"):
        CornerMap.load(degen)


def test_verify_map_directions_flags_undetermined_corner(synthetic_lap):
    """verify_map_directions marks out-of-range corners as undetermined."""
    from simcoach.corners import Corner, CornerMap, verify_map_directions

    # Create a map with an out-of-range corner
    out_of_range_corner = Corner(
        number=3, name="Out of Range", direction="left",
        start_dist=5000, apex_dist=5100, end_dist=5200
    )
    corners = list(CornerMap.load("simcoach/tracks/test_track.json").corners)
    corners.append(out_of_range_corner)
    cmap = CornerMap(corners)

    mismatches = verify_map_directions(synthetic_lap, cmap)
    # Corner 3 declared "left" but computed "undetermined"
    assert (3, "left", "undetermined") in mismatches


def test_corner_map_carries_notes():
    from simcoach.corners import CornerMap
    data = {
        "corners": [{"number": 1, "name": "T1", "direction": "left",
                     "apex_dist": 50.0, "start_dist": 10.0, "end_dist": 90.0}],
        "notes": {"overview": "rhythm track", "corners": {"1": "brake earlier than it looks"},
                  "source": "LMU wiki"},
    }
    cm = CornerMap.from_data(data)
    assert cm.notes["overview"] == "rhythm track"
    assert cm.notes["corners"]["1"].startswith("brake")


def test_corner_map_notes_default_empty():
    from simcoach.corners import CornerMap
    cm = CornerMap.from_data({"corners": []})
    assert cm.notes == {}


def test_laguna_corner_windows_no_longer_overlap():
    """Regression for #30: T7's window bled ~90m into the Corkscrew (T8), and T8
    bled into T9, because windows were a fixed half-width around the apex with no
    clamp against neighboring apexes. Loading the shipped map must now yield
    strictly non-overlapping, apex-ordered windows for every adjacent pair --
    asserted generally (no Laguna-specific numbers), so this also guards every
    other shipped track map against the same class of bug."""
    from simcoach.corners import CornerMap

    cmap = CornerMap.load("simcoach/tracks/weathertech_raceway_laguna_seca.json")
    by_apex = sorted(cmap.corners, key=lambda c: c.apex_dist)
    for prev, cur in itertools.pairwise(by_apex):
        assert prev.end_dist <= cur.start_dist, (
            f"corner {prev.number} end ({prev.end_dist}) overlaps "
            f"corner {cur.number} start ({cur.start_dist})"
        )
        # the apex itself must still be inside the (possibly clamped) window
        assert prev.start_dist <= prev.apex_dist <= prev.end_dist
        assert cur.start_dist <= cur.apex_dist <= cur.end_dist


def _signed_circular_gap(prev_end: float, next_start: float, length: float) -> float:
    """Gap from prev's end to next's start, going the short way around the lap,
    kept in (-length/2, length/2] so a genuine overlap reads as a small negative
    number instead of wrapping around into a huge, falsely-passing 'gap'."""
    return ((next_start - prev_end + length / 2) % length) - length / 2


def test_shipped_track_json_on_disk_has_no_overlapping_windows():
    """#30: every existing test reaches simcoach/tracks/*.json through
    CornerMap.load, which re-applies clamp_corner_windows on load and so masks
    any regression baked into the file itself (test_laguna_corner_windows_no_
    longer_overlap and test_all_shipped_track_files_still_load both pass on the
    file whether or not the on-disk windows are actually clamped). This reads
    the RAW JSON -- no CornerMap involved -- so it checks what's actually on
    disk, for every shipped map, including the S/F seam pair (circularly)."""
    for f in glob.glob("simcoach/tracks/*.json"):
        with open(f) as fh:
            data = json.load(fh)
        length = data.get("length") or data.get("length_m")
        if not length:
            continue  # e.g. test_track.json: no length means no circular clamp
        length = float(length)
        corners = data["corners"]
        by_apex = sorted(corners, key=lambda c: c["apex_dist"])
        n = len(by_apex)
        for k in range(n):
            cur, nxt = by_apex[k], by_apex[(k + 1) % n]
            gap = _signed_circular_gap(cur["end_dist"], nxt["start_dist"], length)
            assert gap >= -1e-6, (
                f"{f}: corner {cur['number']} end ({cur['end_dist']}) overlaps "
                f"corner {nxt['number']} start ({nxt['start_dist']}) by {-gap:.1f} m"
            )
        for c in corners:
            s, e, a = c["start_dist"], c["end_dist"], c["apex_dist"]
            if s <= e:
                assert s <= a <= e, f"{f}: corner {c['number']} apex {a} outside [{s}, {e}]"
            else:  # window wraps the S/F seam
                assert a >= s or a <= e, f"{f}: corner {c['number']} apex {a} outside wrapped [{s}, {e}]"


def test_clamp_preserves_seam_wrap_corner():
    """A corner whose window wraps the S/F seam (start_dist > end_dist) must keep
    wrapping after the overlap clamp -- the clamp only trims the side that bleeds
    past a neighbor's apex, computed with wrap-aware (circular) distance."""
    from simcoach.corners import CornerMap

    data = {
        "track": "seam_test", "length": 1000.0,
        "corners": [
            {"number": 1, "name": "B", "direction": "left",
             "start_dist": 60, "apex_dist": 100, "end_dist": 140},
            {"number": 2, "name": "C", "direction": "left",
             "start_dist": 450, "apex_dist": 500, "end_dist": 550},
            # wraps the seam: start (900) > end (50)
            {"number": 3, "name": "A", "direction": "left",
             "start_dist": 900, "apex_dist": 950, "end_dist": 50},
        ],
    }
    cmap = CornerMap.from_data(data)
    seam = next(c for c in cmap.corners if c.number == 3)
    assert seam.start_dist > seam.end_dist  # still a wrap, not flattened
    assert seam.start_dist == 900  # untouched: well clear of corner C's midpoint
    assert seam.end_dist == 25.0   # trimmed to the midpoint with corner B (apex 100)


def test_clamp_never_cuts_an_apex_out_of_its_window():
    """The overlap clamp must never trim a boundary past one of the corner's OWN
    apexes. A double-apex corner's window has to hold both apexes, so each side is
    guarded against the outermost apex on that side (earliest for start_dist, latest
    for end_dist) -- not against apex_dist, which sits between them. Guarding on
    apex_dist alone silently drops the head/tail apex out of the window from_data
    had just validated."""
    from simcoach.corners import CornerMap, in_window

    data = {
        "track": "double_apex", "length": 1000.0,
        "corners": [
            {"number": 1, "name": "A", "direction": "left",
             "start_dist": 360, "apex_dist": 400, "end_dist": 440},
            # double-apex: the window must keep BOTH 440 and 565, yet the midpoints
            # to its neighbours' apexes (450 back, 560 forward) fall inside that span
            {"number": 2, "name": "B", "direction": "left",
             "start_dist": 420, "apex_dist": 500, "end_dist": 580,
             "apexes": [440, 565]},
            {"number": 3, "name": "C", "direction": "left",
             "start_dist": 600, "apex_dist": 620, "end_dist": 660},
        ],
    }
    cmap = CornerMap.from_data(data)
    for c in cmap.corners:
        for a in c.apexes:
            assert in_window(a, c.start_dist, c.end_dist), (
                f"corner {c.number}: apex {a} clamped out of window "
                f"[{c.start_dist}, {c.end_dist}]"
            )


def test_clamp_rounding_never_pushes_an_apex_outside_its_window():
    """The clamp rounds the midpoint to 0.1 m, so it must compare the ROUNDED
    candidate -- testing the unrounded midpoint and then assigning the rounded one
    can move a boundary past an apex by up to 0.05 m. Apexes carry sub-0.1 precision
    as soon as a user annotation overlay edits them, so this is reachable."""
    from simcoach.corners import CornerMap, in_window

    data = {
        "track": "precision", "length": 1000.0,
        "corners": [
            {"number": 1, "name": "A", "direction": "left",
             "start_dist": 420, "apex_dist": 439.96, "end_dist": 460},
            # the midpoint back to corner 1 is 439.97, which ROUNDS to 440.0 -- past
            # this corner's own apex at 439.98
            {"number": 2, "name": "B", "direction": "left",
             "start_dist": 300, "apex_dist": 439.98, "end_dist": 500},
            {"number": 3, "name": "C", "direction": "left",
             "start_dist": 650, "apex_dist": 700, "end_dist": 750},
        ],
    }
    cmap = CornerMap.from_data(data)
    c2 = next(c for c in cmap.corners if c.number == 2)
    assert in_window(c2.apex_dist, c2.start_dist, c2.end_dist), (
        f"apex {c2.apex_dist} rounded out of window [{c2.start_dist}, {c2.end_dist}]"
    )


def test_corner_map_carries_baked_geometry():
    # Baked geometry metrics (radius_m/width_m/banking_deg/elev_delta_m) live in
    # the map JSON alongside corner-identity fields but aren't Corner fields --
    # from_data must keep them reachable (issue #35), keyed by corner label.
    from simcoach.corners import CornerMap
    data = {
        "corners": [
            {"number": 1, "name": "T1", "direction": "left", "apex_dist": 50.0,
             "start_dist": 10.0, "end_dist": 90.0,
             "radius_m": 66.0, "width_m": 12.0, "banking_deg": 2.0, "elev_delta_m": -11.0},
            {"number": 2, "name": "T2", "direction": "right", "apex_dist": 150.0,
             "start_dist": 110.0, "end_dist": 190.0},  # no geometry for this one
        ],
    }
    cm = CornerMap.from_data(data)
    assert cm.geometry["1"] == {"radius_m": 66.0, "width_m": 12.0,
                                 "banking_deg": 2.0, "elev_delta_m": -11.0}
    assert "2" not in cm.geometry


def test_corner_map_geometry_default_empty():
    from simcoach.corners import CornerMap
    cm = CornerMap.from_data({"corners": []})
    assert cm.geometry == {}
