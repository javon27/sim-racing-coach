"""#169 slice 1: the time on the table. Algarve #244/#250 ("on my best lap, where can I
save 3 tenths?") compared the best lap with itself; Long Beach #237 compared a 1:23
with a 1:36 exploration lap and called it "session best"."""
import dataclasses
import re

import numpy as np
import pytest

from simcoach.analysis import (
    _FORM_FLOOR_S,
    analyze_lap,
    corner_form,
    form_segments,
    optimal_sectors,
    phase_split,
)
from simcoach.coach import AGENTIC_SYSTEM_PROMPT, VOICE_PTT, Coach
from simcoach.coach_tools import TOOLS, CoachContext
from simcoach.corners import Corner, CornerMap
from simcoach.format import format_corner_line, format_time_on_table
from simcoach.model.fake import FakeModel
from simcoach.store import LapStore

CMAP = "simcoach/tracks/test_track.json"   # T1 800-1200 apex 1000, T2 2000-2400 apex 2200
SID = "s169--online"


def _variant(lap, scale=(), **kw):
    """A copy of `lap` with its speed scaled by f over each (lo, hi, f) span -- time
    injected (f < 1) or taken out (f > 1) exactly there, via the ds/v time axis."""
    ch = {k: v.copy() for k, v in lap.channels.items()}
    d = ch["lap_distance"]
    for lo, hi, f in scale:
        ch["speed"][(d >= lo) & (d <= hi)] *= f
    return dataclasses.replace(lap, channels=ch, **kw)


# --- optimal_sectors ------------------------------------------------------------

def test_optimal_sectors_takes_each_sector_from_its_best_clean_lap():
    bounds = [1000.0, 2000.0]
    laps = [
        {"key": "this", "sectors": [30.0, 31.0, 32.0], "incidents": []},
        # The fastest S1 of all, but the lap ran off INSIDE S1 (500 m): it can't count.
        {"key": "A", "sectors": [29.0, 31.5, 32.5], "incidents": [500.0]},
        {"key": "B", "sectors": [29.6, 31.2, 32.2], "incidents": []},
        {"key": "C", "sectors": [30.3, 30.5, 32.4], "incidents": []},
        {"key": "D", "sectors": [30.4, 31.3, 31.4], "incidents": []},
    ]
    out = optimal_sectors(laps, bounds)
    assert [k for _, k in out["best"]] == ["B", "C", "D"]    # three different laps
    # Keeping A's S1 would say 90.9; excluding it is 91.5.
    assert out["total"] == pytest.approx(29.6 + 30.5 + 31.4)
    assert out["dropped"] == [(0, 29.0, "A")]


def test_optimal_sectors_an_incident_only_costs_its_own_sector():
    # A's off is in S3 (2500 m): its S1 is still the best S1.
    laps = [{"key": "this", "sectors": [30.0, 31.0, 32.0], "incidents": []},
            {"key": "A", "sectors": [29.0, 31.5, 31.0], "incidents": [2500.0]}]
    out = optimal_sectors(laps, [1000.0, 2000.0])
    assert out["best"] == [(29.0, "A"), (31.0, "this"), (32.0, "this")]
    assert out["dropped"] == [(2, 31.0, "A")]
    # With no sector bounds the incident can't be placed: the whole lap drops out.
    assert optimal_sectors(laps, None)["best"] == [(30.0, "this"), (31.0, "this"),
                                                   (32.0, "this")]


# --- corner_form ------------------------------------------------------------------

def test_corner_form_ranks_the_repeatable_segment_over_a_one_lap_fluke(synthetic_lap):
    cmap = CornerMap.load(CMAP)
    slow_t1 = (850.0, 1150.0, 0.99)
    subject = _variant(synthetic_lap, [slow_t1])
    others = (
        [(f"quick{i}", _variant(synthetic_lap), []) for i in range(2)]      # 2 laps quicker at T1
        + [(f"level{i}", _variant(synthetic_lap, [slow_t1]), []) for i in range(4)]
        # ONE lap far quicker at T2 -- a bigger gain than T1's, but only once.
        + [("fluke", _variant(synthetic_lap, [slow_t1, (2050.0, 2350.0, 1.05)]), [])])
    form = corner_form(subject, others, cmap)
    assert form["ok"] and form["n"] == 7
    # The fluke is the larger single gain (next test), so ranking or listing by the
    # quickest lap would put T2 first; two laps quicker is what lists T1, and T1 only.
    assert [r["corner"].number for r in form["listed"]] == [1]
    t1 = form["listed"][0]
    assert t1["repeatable"] <= -_FORM_FLOOR_S and t1["donor"].startswith("quick")


def test_corner_form_fluke_really_is_larger(synthetic_lap):
    # Guards the test above against a fixture that doesn't inject what it claims.
    cmap = CornerMap.load(CMAP)
    slow_t1 = (850.0, 1150.0, 0.99)
    subject = _variant(synthetic_lap, [slow_t1])
    fluke = _variant(synthetic_lap, [slow_t1, (2050.0, 2350.0, 1.05)])
    quick = _variant(synthetic_lap)
    t1_seg, t2_seg = [s[1:] for s in form_segments(cmap)]
    t1 = phase_split(subject, quick, cmap.corners[0], t1_seg)
    t2 = phase_split(subject, fluke, cmap.corners[1], t2_seg)
    assert sum(t2.values()) > sum(t1.values()) > _FORM_FLOOR_S


def test_corner_form_skips_a_lap_with_an_incident_in_the_segment(synthetic_lap):
    cmap = CornerMap.load(CMAP)
    slow_t1 = (850.0, 1150.0, 0.99)
    subject = _variant(synthetic_lap, [slow_t1])
    others = ([("quick0", _variant(synthetic_lap), []),
               ("quick1", _variant(synthetic_lap), [1000.0])]      # ran off AT T1
              + [(f"level{i}", _variant(synthetic_lap, [slow_t1]), []) for i in range(4)])
    form = corner_form(subject, others, cmap)
    assert form["ok"]
    # One clean quick lap left at T1: not "two of your laps", so not listed.
    assert all(r["corner"].number != 1 for r in form["listed"])


def test_corner_form_needs_five_laps(synthetic_lap):
    cmap = CornerMap.load(CMAP)
    others = [(f"L{i}", _variant(synthetic_lap), []) for i in range(4)]
    assert corner_form(synthetic_lap, others, cmap) == {"ok": False, "n": 4}


# --- phase_split -----------------------------------------------------------------

def test_phase_split_entry_only_loss_lands_in_entry(synthetic_lap):
    cmap = CornerMap.load(CMAP)
    t1, a, b = form_segments(cmap)[0]
    subject = _variant(synthetic_lap, [(810.0, 990.0, 0.97)])   # inside [start, apex]
    ph = phase_split(subject, synthetic_lap, t1, (a, b))
    assert max(ph, key=ph.get) == "entry" and ph["entry"] > 0.05
    assert all(abs(ph[p]) < 0.005 for p in ("approach", "exit", "run-out"))
    # ...and the same loss just past the apex is the exit's: the cut is the apex.
    ph = phase_split(_variant(synthetic_lap, [(1010.0, 1190.0, 0.97)]), synthetic_lap, t1, (a, b))
    assert max(ph, key=ph.get) == "exit" and abs(ph["entry"]) < 0.005


def test_phase_split_exit_gain_given_back_on_the_run_out_shows_both(synthetic_lap):
    # The race-lap-9 T14 shape: quicker from the apex to the exit, slower after it.
    cmap = CornerMap.load(CMAP)
    t1, a, b = form_segments(cmap)[0]
    subject = _variant(synthetic_lap, [(1010.0, 1190.0, 1.05), (1210.0, 1990.0, 0.98)])
    ph = phase_split(subject, synthetic_lap, t1, (a, b))
    assert ph["exit"] < -_FORM_FLOOR_S and ph["run-out"] > _FORM_FLOOR_S
    row = {"corner": t1, "seg": (a, b), "donor": 7, "donor_d": -sum(ph.values()),
           "typical": 0.0, "repeatable": -sum(ph.values()), "spread": 0.0}
    you = analyze_lap(subject, cmap).corners[0]
    text = format_time_on_table(
        lap_time=92.5, sectors=None, optimal=None, all_time=None, benchmark=None,
        form={"ok": True, "n": 5, "listed": [row], "at_best": []},
        diagnoses=[{"row": row, "phases": ph, "you": you, "them": you,
                    "next": cmap.corners[1]}],
        label_of=lambda k: "race lap 9", cmap=cmap, bounds=None)
    line = next(ln for ln in text.splitlines() if ln.startswith("T1 Left Hairpin vs race lap 9"))
    assert "on the run to T2 after the exit" in line
    assert "gained" in line and "from the apex to the end of the corner" in line


# --- 2.0: brake metrics ------------------------------------------------------------

def test_peak_brake_is_the_braking_runs_peak_when_braked_before_the_window(synthetic_lap):
    # T2 is braked in a straight line and released at 2140 (conftest). A window
    # starting after that holds no braking at all -- the old window-only peak was 0.
    lap = _variant(synthetic_lap)
    d = lap.channels["lap_distance"]
    lap.channels["brake"][d >= 2140.0] = 0.0
    cmap = CornerMap([Corner(number=2, name="Right Sweeper", direction="right",
                             start_dist=2150.0, apex_dist=2200.0, end_dist=2400.0)])
    cm = analyze_lap(lap, cmap).corners[0]
    run = lap.channels["brake"][(d >= 2000.0) & (d < 2140.0)]
    assert cm.brake_point is not None
    assert cm.peak_brake == pytest.approx(float(run.max())) and cm.peak_brake > 0.5
    assert "@ 0% peak" not in format_corner_line(cm)


def test_brake_zone_start_survives_a_mid_zone_pressure_dip(synthetic_lap):
    cmap = CornerMap.load(CMAP)
    clean = analyze_lap(synthetic_lap, cmap).corners[0]
    lap = _variant(synthetic_lap)
    d, brake = lap.channels["lap_distance"], lap.channels["brake"]
    run = np.flatnonzero((brake > 0.1) & (d <= 1000.0) & (d >= 900.0))
    brake[run[len(run) // 2]] = 0.05          # released under 0.1 once, then re-applied
    dipped = analyze_lap(lap, cmap).corners[0]
    assert dipped.brake_point > clean.brake_point          # the dip moves brake_point...
    assert dipped.brake_zone_start == clean.brake_zone_start   # ...not the zone start
    assert clean.brake_zone_start == pytest.approx(clean.brake_point)
    assert dipped.brake_zone_peak == pytest.approx(clean.brake_zone_peak)


def test_peak_brake_survives_a_modulated_run(synthetic_lap):
    # Real lap 7590 T10: 82% stop, released under 0.1 before the apex, re-applied at
    # 11% -- the run brake_point walks is only the re-application ("@ 11% peak").
    cmap = CornerMap.load(CMAP)
    lap = _variant(synthetic_lap)
    d, brake = lap.channels["lap_distance"], lap.channels["brake"]
    lap.channels["throttle"][(d >= 880.0) & (d <= 1000.0)] = 0.0   # one braking zone
    brake[(d >= 880.0) & (d < 960.0)] = 0.82
    brake[(d >= 960.0) & (d < 985.0)] = 0.07
    brake[(d >= 985.0) & (d <= 1200.0)] = np.minimum(brake[(d >= 985.0) & (d <= 1200.0)], 0.11)
    cm = analyze_lap(lap, cmap).corners[0]
    assert cm.apex_dist - cm.brake_point < 20          # the run is the re-application
    assert cm.peak_brake == pytest.approx(0.82) and cm.brake_zone_peak == pytest.approx(0.82)
    assert "@ 82% peak" in format_corner_line(cm)


def test_brake_zone_never_reaches_back_into_the_previous_corner(synthetic_lap):
    # Real Algarve lap 7625 T8 "braking from 209 m", Laguna T9 at 175 m: throttle never
    # reached 0.8 between two corners, so the zone ran back into the previous one's stop.
    lap = _variant(synthetic_lap)
    d = lap.channels["lap_distance"]
    between = (d >= 1000.0) & (d <= 1300.0)
    lap.channels["throttle"][between] = np.minimum(lap.channels["throttle"][between], 0.5)
    own = (d >= 1260.0) & (d <= 1290.0)
    lap.channels["brake"][own] = 0.6
    lap.channels["throttle"][own] = 0.0
    cmap = CornerMap([
        Corner(number=1, name="", direction="left", start_dist=800.0, apex_dist=1000.0,
               end_dist=1200.0),
        Corner(number=2, name="", direction="left", start_dist=1250.0, apex_dist=1300.0,
               end_dist=1400.0)])
    cm = analyze_lap(lap, cmap).corners[1]
    assert cm.brake_zone_start >= 1200.0                 # not T1's stop at ~1000 m
    assert cm.brake_zone_peak == pytest.approx(0.6)      # not T1's ~97%
    assert cm.peak_brake == pytest.approx(0.6)


# --- reference honesty (through the store) ------------------------------------------

def _store(tmp_path):
    return LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))


def _ctx(store, cmap=None, sid=SID, **kw):
    return CoachContext(store, cmap=cmap or CornerMap.load(CMAP), track="test_track",
                        car="test_car", session_id=sid, on_track=False, **kw)


def _save(store, lap, lap_time, ts, lap_no, phase="practice", sid=SID, **kw):
    return store.save(dataclasses.replace(lap, lap_time=lap_time, timestamp=float(ts),
                                          session_id=sid, phase=phase, lap_no=lap_no, **kw))


def test_best_is_this_sessions_best_not_the_all_time_best(tmp_path, synthetic_lap):
    # Red-team probe1/probe6 A: 'best' was the all-time best, a lap from ANOTHER
    # session printed as "Lap: this lap (...)" with its sectors "not stored".
    store = _store(tmp_path)
    other = _save(store, synthetic_lap, 88.0, 1, 1, sid="OTHER",
                  sectors=[29.0, 29.5, 29.5])                                  # all-time best
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 91.0, 10, 1)
    _save(store, synthetic_lap, 90.0, 11, 2)                                   # session best
    ctx = _ctx(store)
    assert ctx.get_time_on_table("best").startswith("Lap: practice lap 2 ")
    out = ctx.get_lap_summary("best")
    assert out.startswith("Lap: practice lap 2 ")
    assert "This is your best lap this session: get_time_on_table finds the time" in out
    # A session with no valid timed lap of its own falls back to the all-time best --
    # named as a lap from another session, never "this lap", and with its sectors.
    _save(store, synthetic_lap, 87.0, 20, 1, sid="EMPTY", valid=False)
    ctx = _ctx(store, sid="EMPTY")
    record, lap_id = ctx._resolve_lap("best")
    assert lap_id == other and record.sectors == [29.0, 29.5, 29.5]
    line = ctx.get_time_on_table("best").splitlines()[0]
    assert line.startswith("Lap: a lap from another session (game lap 1, 19") and \
        "this lap" not in line


def test_an_invalid_fastest_lap_is_never_called_your_best(tmp_path, synthetic_lap):
    # probe6 B: an invalid 89.0 against a valid 92.0 was "your best lap".
    store = _store(tmp_path)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 92.0, 1, 1)
    _save(store, synthetic_lap, 89.0, 2, 2, valid=False)
    out = _ctx(store).get_lap_summary("latest")
    assert "This is your best lap" not in out and "next-best" not in out
    assert "vs your session best" in out


def test_no_comparable_wording_names_what_is_slower(tmp_path, synthetic_lap):
    # probe6 C: the fastest lap was told "your all-time best is 16% slower".
    store = _store(tmp_path)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.5)]), 104.0, 1, 1, sid="OTHER")
    _save(store, synthetic_lap, 90.0, 2, 1)
    assert _ctx(store).get_time_loss() == \
        "No comparable lap yet (your other laps here are 16%+ slower)."


def test_race_start_as_the_only_other_lap_says_so(tmp_path, synthetic_lap):
    # probe6 D: "you need a second valid lap" with race lap 1 sitting right there.
    store = _store(tmp_path)
    _save(store, synthetic_lap, 89.0, 1, 1, phase="race")
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.95)]), 91.0, 2, 2, phase="race")
    assert _ctx(store).get_time_loss() == (
        "No comparable lap yet (the only other lap is the race start, race lap 1, which "
        "isn't a fair comparison).")


def test_get_corner_names_the_reference_its_deltas_are_against(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 92.0, 2, 2)
    out = _ctx(store).get_corner("1", "best")
    assert "min vs best" in out
    assert "Deltas ('vs best') are vs your next-best lap (practice lap 2, +2.00 s)." in out


@pytest.mark.parametrize("cls, refs, want", [
    ("LMP2_ELMS", {"LMP2": "1:32.936"}, None),           # not a substring match
    ("GT3", {"LMGT3": "1:42.879", "GTE": "1:41.489"}, "LMGT3"),
    ("GT3", {"GT3": "1:42.000", "LMGT3": "1:42.879"}, "GT3"),    # exact wins
    ("Hyper", {"Hypercar": "1:31.878"}, "Hypercar"),
    ("LMP2", {"LMP2": "1:32.936", "LMP3": "1:39.100"}, "LMP2"),
    ("lm gt3", {"LMGT3": "1:42.879"}, "LMGT3"),
])
def test_class_benchmark_matches_the_class_not_a_substring(tmp_path, cls, refs, want):
    ctx = _ctx(_store(tmp_path), car_class=cls)
    ctx.cmap.notes = {"reference_times": refs}
    got = ctx._class_benchmark()
    assert (got[0] if got else None) == want


def test_class_benchmark_matches_the_scraped_class_not_the_rest_one(tmp_path):
    # #190: REST may call the ELMS Oreca 'LMP2'; Sarthe's WEC LMP2 time is not its
    # benchmark. The scraped class (match_class) is matched; REST is only shown.
    ctx = _ctx(_store(tmp_path), car_class="LMP2", match_class="LMP2_ELMS")
    ctx.cmap.notes = {"reference_times": {"Hypercar": "3:12.881", "LMGT3": "3:55.763",
                                          "LMP2": "3:25.285", "LMP3": "3:44.599"}}
    assert ctx._class_benchmark() is None


def test_lap_summary_best_is_never_compared_with_itself(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)                                    # the best
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 92.0, 2, 2)   # slower at T1
    out = _ctx(store).get_lap_summary("best")
    # #173: 'best' resolves its lap id -- the lap is named, not "this lap".
    assert out.startswith("Lap: practice lap 1 ")
    assert "Net -0.00s" not in out and "Net" not in out     # no empty time-loss line
    assert "This is your best lap: get_time_on_table finds the time left in it." in out
    deltas = re.findall(r"min vs best: ([+-]\d+\.\d)", out)
    assert deltas and any(float(x) != 0.0 for x in deltas)
    assert "vs your next-best lap (practice lap 2, +2.00 s)" in out


def test_best_latest_lap_is_labelled_next_best(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 92.0, 1, 1)
    _save(store, synthetic_lap, 90.0, 2, 2)                     # the latest IS the best
    out = _ctx(store).get_lap_summary("latest")
    assert "Deltas below are vs your next-best lap (practice lap 1, +2.00 s)" in out
    assert "session best" not in out and "driver's best lap" not in out


def test_a_lap_over_107_percent_is_never_the_reference(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.5)]), 104.0, 1, 1)   # +15.6%
    _save(store, synthetic_lap, 90.0, 2, 2)
    ctx = _ctx(store)
    out = ctx.get_lap_summary("latest")
    assert "No comparable lap yet (practice lap 1 is 16% slower)." in out
    assert "min vs best" not in out and "Deltas below" not in out
    assert ctx.get_time_loss() == "No comparable lap yet (practice lap 1 is 16% slower)."
    assert ctx.get_time_on_table("latest").endswith(
        "No comparable lap yet (practice lap 1 is 16% slower).")


def test_race_lap_1_and_pit_laps_are_never_the_reference(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, synthetic_lap, 89.0, 1, 1, phase="race")              # the start
    _save(store, synthetic_lap, 89.5, 2, 2, phase="race", pit=True)    # a stop
    _save(store, synthetic_lap, 91.0, 3, 3, phase="race")
    _save(store, synthetic_lap, 90.0, 4, 4, phase="race")
    ref, label = _ctx(store)._reference_lap(store.load(4), 4)
    assert ref.lap_time == 91.0 and label.startswith("your next-best lap (race lap 3")


# --- get_time_on_table end to end ----------------------------------------------------

def _session(tmp_path, synthetic_lap, n_level=4):
    """The subject (best, latest) loses time at T1 to two laps; `n_level` laps match
    it there; one more lap has the fastest S1 but ran off at T1."""
    store = _store(tmp_path)
    slow_t1, slow_end = (850.0, 1150.0, 0.99), (2500.0, 2900.0, 0.99)
    bounds = [1500.0, 2600.0]
    laps = ([(_variant(synthetic_lap, [slow_end]), 92.8, [30.40, 31.30, 31.10])] * 2
            + [(_variant(synthetic_lap, [slow_t1, slow_end]), 92.9 + i / 10,
                [30.60, 31.20 - i / 100, 31.10]) for i in range(n_level)])
    off = _variant(synthetic_lap, [slow_t1])
    t1 = (off.channels["lap_distance"] >= 800.0) & (off.channels["lap_distance"] <= 1200.0)
    for w in ("fl", "fr", "rl", "rr"):   # #207: an off is two wheels on the grass
        off.channels[f"surface_{w}"] = t1 * (2.0 if w in ("fl", "rl") else 0.0)
    laps.append((off, 93.5, [30.20, 31.40, 31.20]))
    for i, (lap, t, sec) in enumerate(laps, start=1):
        _save(store, lap, t, i, i, sectors=sec, sector_bounds=bounds)
    n = len(laps) + 1
    _save(store, _variant(synthetic_lap, [slow_t1]), 92.7, n, n,
          sectors=[30.60, 31.00, 31.10], sector_bounds=bounds)
    return store


def test_get_time_on_table_finds_the_time_on_the_best_lap(tmp_path, synthetic_lap):
    store = _session(tmp_path, synthetic_lap)
    ctx = _ctx(store, car_class="GT3")
    ctx.cmap.notes = {"reference_times": {"LMGT3": "1:30.000", "GTE": "1:29.000"}}
    out = ctx.dispatch("get_time_on_table", {})
    lines = out.splitlines()
    assert lines[0].startswith("Lap: practice lap 8")
    # Headline: the game-sector optimal lap -- S1 from lap 1, never the off lap's 30.20.
    assert lines[1] == ("Optimal lap from your best sectors this session: 1:32.500, 0.20 s "
                        "under this lap (your own sectors only; not a ceiling on pace):")
    assert lines[2] == "  S1 (T1) 0.20 s on practice lap 1."
    assert "Quicker S1 (T1) on practice lap 7 doesn't count: a spin, a stop or an off" in out
    # All-time: the same as this session's optimal (no other session), so not
    # repeated -- and never the 1:32.300 the off lap's 30.20 S1 would make.
    assert "All-time here in this car: best 1:32.700 (this lap)." in out
    assert ("Class benchmark (track notes, community): LMGT3 1:30.000, 2.70 s ahead of "
            "this lap. Total only, no sector splits, so it can't say where.") in out
    assert "Where your own laps were quicker than this one (corner with approach and " \
           "run-out, 7 laps):" in out
    assert re.search(r"  T1 Left Hairpin: [^\n]*two laps 0\.\d\d s\+ quicker "
                     r"\(practice lap [12] by 0\.\d\d s\)", out)
    assert re.search(r"T1 Left Hairpin vs practice lap [12]: 0\.\d\d s, 0\.\d\d of it ", out)
    assert "At your best on this lap" in out
    assert "error" not in out


def test_get_time_on_table_detail_diagnoses_more_corners(tmp_path, synthetic_lap):
    # Four corners, and the subject loses time in every one of them to two laps: four
    # listed segments, so the 2-vs-3 cap is what decides how many get a diagnosis.
    cmap = CornerMap([Corner(number=n, name="", direction="left", start_dist=a,
                             apex_dist=(a + b) / 2, end_dist=b)
                      for n, (a, b) in enumerate(((300.0, 500.0), (800.0, 1200.0),
                                                  (2000.0, 2400.0), (2600.0, 2800.0)), 1)])
    slow = [(a + 20, b - 20, 0.98) for a, b in ((300, 500), (800, 1200), (2000, 2400),
                                                 (2600, 2800))]
    store = _store(tmp_path)
    for i in range(2):
        _save(store, synthetic_lap, 92.0 + i / 10, i + 1, i + 1)
    for i in range(4):
        _save(store, _variant(synthetic_lap, slow), 93.0 + i / 10, i + 3, i + 3)
    _save(store, _variant(synthetic_lap, slow), 92.9, 7, 7)
    short = _ctx(store, cmap=cmap).get_time_on_table("latest")
    long = _ctx(store, cmap=cmap).dispatch("get_time_on_table",
                                           {"which": "latest", "detail": True})
    assert short.count("s+ quicker") == 4
    assert short.count(" of it ") == 2
    assert long.count(" of it ") == 3


def test_get_time_on_table_needs_five_comparable_laps(tmp_path, synthetic_lap):
    store = _session(tmp_path, synthetic_lap, n_level=0)   # 3 other laps
    out = _ctx(store).get_time_on_table()
    assert "Not enough clean laps near this pace to read your form (3 of 5)." in out
    assert "Optimal lap from your best sectors" in out


# --- #176: get_optimal_seconds (the lap rail's Optimal row) --------------------------

def test_get_optimal_seconds_matches_the_stated_headline(tmp_path, synthetic_lap):
    # Same session `test_get_time_on_table_finds_the_time_on_the_best_lap` reads: it
    # has a lap with the fastest S1 that ran off at T1 -- the naive min-of-sectors
    # would use that 30.20 and understate the real total by 0.20 s.
    store = _session(tmp_path, synthetic_lap)
    ctx = _ctx(store)
    prose = ctx.get_time_on_table()
    m = re.search(r"Optimal lap from your best sectors this session: (\d+):(\d+\.\d+),", prose)
    assert m, prose
    stated = int(m.group(1)) * 60 + float(m.group(2))
    assert ctx.get_optimal_seconds() == pytest.approx(stated, abs=0.001)
    # Never the number a naive min-of-sectors would give (the off lap's 30.20 S1).
    naive = 92.7 - (92.7 - (30.20 + 31.00 + 31.10))
    assert ctx.get_optimal_seconds() != pytest.approx(naive, abs=0.001)


def test_get_optimal_seconds_none_when_subjects_own_sector_dropped(tmp_path, synthetic_lap):
    # The BEST lap itself ran off in its own fastest sector: get_time_on_table prints
    # gains only, never a total resting on that sector -- get_optimal_seconds must
    # agree there is no honest headline figure.
    store = _store(tmp_path)
    bounds = [1500.0, 2600.0]
    off = _variant(synthetic_lap, [])
    t1 = (off.channels["lap_distance"] >= 800.0) & (off.channels["lap_distance"] <= 1200.0)
    for w in ("fl", "fr", "rl", "rr"):   # #207: an off is two wheels on the grass
        off.channels[f"surface_{w}"] = t1 * (2.0 if w in ("fl", "rl") else 0.0)
    _save(store, off, 90.0, 1, 1, sectors=[29.0, 31.0, 31.0], sector_bounds=bounds)
    _save(store, synthetic_lap, 91.0, 2, 2, sectors=[30.0, 30.8, 31.2], sector_bounds=bounds)
    ctx = _ctx(store)
    assert "This lap's own S1 (T1) had a spin, a stop or an off" in ctx.get_time_on_table()
    assert ctx.get_optimal_seconds() is None


def test_get_optimal_seconds_none_cases(tmp_path, synthetic_lap):
    # No corner map: the full tool refuses too -- #176 can't honestly do better.
    store = _store(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)
    no_map = CoachContext(store, cmap=None, track="test_track", car="test_car",
                          session_id=SID, on_track=False)
    assert no_map.get_optimal_seconds() is None
    # No comparable lap this session (only one lap stored).
    assert _ctx(store).get_optimal_seconds() is None
    # No such lap at all.
    assert _ctx(_store(tmp_path)).get_optimal_seconds() is None


def test_nothing_measurable_says_so():
    cmap = CornerMap.load(CMAP)
    text = format_time_on_table(
        lap_time=92.5, sectors=[30.0, 31.0, 31.5],
        optimal={"total": 92.5, "best": [(30.0, "this"), (31.0, "this"), (31.5, "this")],
                 "dropped": []},
        all_time=None, benchmark=None,
        form={"ok": True, "n": 6, "listed": [],
              "at_best": [{"corner": c} for c in cmap.corners]},
        diagnoses=[], label_of=str, cmap=cmap, bounds=[1500.0, 2600.0])
    assert "this lap (every sector is your best)" in text
    assert "No corner where another lap of yours was measurably quicker" in text
    assert "At your best on this lap (no other lap quicker by 0.03 s): T1, T2." in text


def _table(lap_time, sectors, laps):
    cmap = CornerMap.load(CMAP)
    bounds = [1500.0, 2600.0]
    return format_time_on_table(
        lap_time=lap_time, sectors=sectors, optimal=optimal_sectors(laps, bounds),
        all_time=None, benchmark=None, form={"ok": True, "n": 5, "listed": [], "at_best": []},
        diagnoses=[], label_of=lambda k: "this lap" if k == "this" else f"lap {k}",
        cmap=cmap, bounds=bounds)


def test_own_sector_with_an_off_is_the_baseline_never_a_negative_gain():
    # probe8: this lap's quickest S1 had an off. It said "every sector is your best",
    # then "Quicker S1 on this lap doesn't count" -- or "-0.40 s under this lap".
    text = _table(91.5, [29.5, 31.0, 31.0], [
        {"key": "this", "sectors": [29.5, 31.0, 31.0], "incidents": [900.0]},
        {"key": "A", "sectors": [30.0, 31.3, 31.4], "incidents": []},
        {"key": "B", "sectors": [30.1, 31.2, 31.3], "incidents": []}])
    assert "no clean sector of yours beat this lap's" in text
    assert "This lap's own S1 (T1) had a spin, a stop or an off: not counted as a best sector." in text
    assert "every sector is your best" not in text and "on this lap doesn't count" not in text
    assert not re.search(r"-\d+\.\d\d s under", text)
    assert "at your own limit everywhere" not in text
    # A real gain elsewhere is measured from this lap's own times: S2 0.20 s -- with
    # NO total, which would rest on the S1 it ran off in (probe12 F: "1:30.500").
    text = _table(91.5, [29.5, 31.0, 31.0], [
        {"key": "this", "sectors": [29.5, 31.0, 31.0], "incidents": [900.0]},
        {"key": "A", "sectors": [30.0, 30.8, 31.4], "incidents": []}])
    assert text.splitlines()[0] == ("0.20 s from your best clean sectors this session: "
                                    "S2 (T2) 0.20 s on lap A.")
    assert "Optimal lap" not in text and "1:31.300" not in text
    assert "This lap's own S1 (T1) had a spin, a stop or an off" in text


def _table_all_time(lap_time, sectors, laps, best, other):
    cmap = CornerMap.load(CMAP)
    bounds = [1500.0, 2600.0]
    return format_time_on_table(
        lap_time=lap_time, sectors=sectors, optimal=optimal_sectors(laps, bounds),
        all_time={"best": best, "this_lap": True, "when": "", "other": other},
        benchmark=None, form={"ok": True, "n": 5, "listed": [], "at_best": []},
        diagnoses=[], label_of=lambda k: "this lap" if k == "this" else f"lap {k}",
        cmap=cmap, bounds=bounds)


def test_all_time_optimal_never_rests_on_this_laps_off_sector():
    laps = [{"key": "this", "sectors": [29.5, 31.0, 31.0], "incidents": [900.0]},
            {"key": "A", "sectors": [30.0, 30.8, 31.4], "incidents": []}]
    # Clean sectors: 30.0 + 30.8 + 31.0 = 91.8, slower than the 91.5 best -- no line.
    # (With the off S1 it would be 29.5 + 30.8 + 31.0 = 91.3 and print.)
    text = _table_all_time(91.5, [29.5, 31.0, 31.0], laps, 91.5, [None, None, None])
    assert "All-time here in this car: best 1:31.500 (this lap)." in text
    # A clean 29.0 S1 from another session: 29.0 + 30.8 + 31.0 = 90.8.
    text = _table_all_time(91.5, [29.5, 31.0, 31.0], laps, 91.5, [29.0, None, None])
    assert "(this lap); optimal from best sectors 1:30.800 (your own sectors only; not a ceiling on pace)." in text


def test_all_time_optimal_is_built_like_the_session_one():
    # Real Algarve LGC: session 1:45.736 vs all-time 1:45.734 -- the session total drops
    # gains under 0.005 s, the all-time one didn't.
    laps = [{"key": "this", "sectors": [30.0, 31.0, 31.0], "incidents": []},
            {"key": "A", "sectors": [29.8, 31.2, 31.2], "incidents": []}]
    for other in ([None, None, None], [None, 30.997, None]):   # a 0.003 s "gain"
        text = _table_all_time(92.0, [30.0, 31.0, 31.0], laps, 92.0, other)
        assert "1:31.800, 0.20 s under this lap" in text
        assert "All-time here in this car: best 1:32.000 (this lap)." in text
    text = _table_all_time(92.0, [30.0, 31.0, 31.0], laps, 92.0, [None, 30.7, None])
    assert "(this lap); optimal from best sectors 1:31.500 (your own sectors only; not a ceiling on pace)." in text


def test_no_session_context_never_says_another_session(tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)
    line = _ctx(store, sid=None).get_lap_summary("best").splitlines()[0]
    assert "another session" not in line


# --- the prompt ----------------------------------------------------------------------

def test_prompt_and_tool_text_send_time_left_questions_to_get_time_on_table(
        tmp_path, synthetic_lap):
    # The fake model is scripted, so this does NOT prove a real model routes there: it
    # proves the wiring (the tool is advertised and dispatches through the agentic
    # loop) and that the text the real model routes on says so, without the
    # get_lap_summary rule claiming the same questions.
    store = _session(tmp_path, synthetic_lap)
    model = FakeModel(reply="Turn 1: ...", tool_script=[("get_time_on_table", {})])
    Coach(model).ask_agentic(_ctx(store), "on my best lap where's 3 tenths", "test_car")
    name, _, result = model.dispatched[0]
    assert name == "get_time_on_table" and "Optimal lap from your best sectors" in result
    assert any(t["name"] == "get_time_on_table" for t in model.last_tools)
    p = model.last_system
    assert "'find me N tenths'" in p and "'where am I losing time'" in p
    assert "or about the BEST lap → get_time_on_table FIRST" in p
    assert "'how was that lap' or 'why was that lap slower' → get_lap_summary FIRST" in p
    summary_rule = p[p.index("'how was that lap'"):p.index("→ get_lap_summary FIRST")]
    assert "losing time" not in summary_rule and "work on" not in summary_rule
    # Never a flat "there is always a reference" that contradicts the tool's own
    # "No comparable lap yet".
    assert "never say there is no reference" not in p
    assert "if it says there's no comparable lap, say that" in p
    spec = next(t for t in TOOLS if t["name"] == "get_time_on_table")
    assert "'find me N tenths'" in spec["description"]
    loss = next(t for t in TOOLS if t["name"] == "get_time_loss")["description"]
    assert "Call this FIRST for 'where am I losing time'" not in loss


def test_prompt_leads_with_the_diagnosis_and_drops_apply_freely():
    p = AGENTIC_SYSTEM_PROMPT
    assert "apply it freely" not in p
    assert "explain and act on what the tools measure, not in place of it" in p
    assert "LEAD WITH THE DIAGNOSIS" in p and "Never invent a fix." in p
    assert "call one a loss only where a time delta in the tool says the time went there" in p
    # PTT: one corner by default -- a default, never a cap (#168 dropped the code cut).
    assert "with ONE corner" in VOICE_PTT and "a default, not a cap" in VOICE_PTT
    assert "give the FULL answer" in VOICE_PTT


def test_best_lap_header_says_deltas_are_vs_a_slower_lap_not_time_lost(tmp_path, synthetic_lap):
    # #236
    store = _store(tmp_path)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 92.0, 1, 1)
    _save(store, synthetic_lap, 90.0, 2, 2)
    out = _ctx(store).get_lap_summary("latest")
    assert "SLOWER lap: the deltas are NOT where time was lost" in out


def test_get_corner_and_summary_best_carry_the_slower_clause_nonbest_does_not(
        tmp_path, synthetic_lap):
    store = _store(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1)
    _save(store, _variant(synthetic_lap, [(900.0, 1100.0, 0.9)]), 92.0, 2, 2)
    ctx = _ctx(store)
    assert "SLOWER lap: the deltas are NOT where time was lost" in ctx.get_corner("1", "best")
    assert "SLOWER lap: the deltas are NOT where time was lost" in ctx.get_lap_summary("best")
    assert "SLOWER lap" not in ctx.get_lap_summary("latest")      # the 92.0 lap is not best


def test_optimal_line_says_own_sectors_not_a_ceiling(tmp_path, synthetic_lap):
    # #235
    store = _store(tmp_path)
    _save(store, synthetic_lap, 90.0, 1, 1, sectors=[29.0, 30.5, 30.5])
    _save(store, synthetic_lap, 91.0, 2, 2, sectors=[30.0, 30.5, 30.5])
    out = _ctx(store).get_time_on_table("latest")
    assert "(your own sectors only; not a ceiling on pace)" in out
