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


# --- reference honesty (through the store) ------------------------------------------

def _store(tmp_path):
    return LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))


def _ctx(store, **kw):
    return CoachContext(store, cmap=CornerMap.load(CMAP), track="test_track", car="test_car",
                        session_id=SID, on_track=False, **kw)


def _save(store, lap, lap_time, ts, lap_no, phase="practice", **kw):
    return store.save(dataclasses.replace(lap, lap_time=lap_time, timestamp=float(ts),
                                          session_id=SID, phase=phase, lap_no=lap_no, **kw))


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
    off.channels["track_edge"][(off.channels["lap_distance"] >= 800.0)
                               & (off.channels["lap_distance"] <= 1200.0)] = 1.0   # off at T1
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
                        "under this lap:")
    assert lines[2] == "  S1 (T1) 0.20 s on practice lap 1."
    assert "Quicker S1 (T1) on practice lap 7 doesn't count: a spin or an off" in out
    # All-time: the off lap's 30.20 S1 stays out here too (1:32.300 with it).
    assert ("All-time here in this car: best 1:32.700 (this lap); optimal from best "
            "sectors 1:32.500.") in out
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
    store = _session(tmp_path, synthetic_lap)
    short = _ctx(store).get_time_on_table()
    long = _ctx(store).dispatch("get_time_on_table", {"detail": True})
    n_listed = short.count("s+ quicker")   # the listed rows bound the diagnoses
    assert n_listed >= 1
    assert short.count(" of it ") == min(2, n_listed)
    assert long.count(" of it ") == min(3, n_listed)


def test_get_time_on_table_needs_five_comparable_laps(tmp_path, synthetic_lap):
    store = _session(tmp_path, synthetic_lap, n_level=0)   # 3 other laps
    out = _ctx(store).get_time_on_table()
    assert "Not enough clean laps near this pace to read your form (3 of 5)." in out
    assert "Optimal lap from your best sectors" in out


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


# --- the prompt ----------------------------------------------------------------------

def test_best_lap_tenths_question_routes_to_get_time_on_table(tmp_path, synthetic_lap):
    store = _session(tmp_path, synthetic_lap)
    model = FakeModel(reply="Turn 1: ...", tool_script=[("get_time_on_table", {})])
    Coach(model).ask_agentic(_ctx(store), "on my best lap where's 3 tenths", "test_car")
    name, _, result = model.dispatched[0]
    assert name == "get_time_on_table" and "Optimal lap from your best sectors" in result
    # What the real model reads to make that call:
    assert "'find me N tenths'" in model.last_system
    assert "any question about the BEST lap → get_time_on_table FIRST" in model.last_system
    assert "never say there is no reference" in model.last_system
    spec = next(t for t in TOOLS if t["name"] == "get_time_on_table")
    assert "'find me N tenths'" in spec["description"]


def test_prompt_leads_with_the_diagnosis_and_drops_apply_freely():
    p = AGENTIC_SYSTEM_PROMPT
    assert "apply it freely" not in p
    assert "explain and act on what the tools measure, not in place of it" in p
    assert "LEAD WITH THE DIAGNOSIS" in p and "Never invent a fix." in p
    assert "call one a loss only where a time delta in the tool says the time went there" in p
    # PTT: one corner by default -- a default, never a cap (#168 dropped the code cut).
    assert "with ONE corner" in VOICE_PTT and "a default, not a cap" in VOICE_PTT
    assert "give the FULL answer" in VOICE_PTT
