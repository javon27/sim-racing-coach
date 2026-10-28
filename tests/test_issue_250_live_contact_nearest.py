"""#250: "who hit me?" seconds after the hit reads the lap IN PROGRESS, which never got
#238's nearest-same-class-car line; and race lap 1 never had a field trace at all.

Fixtures are the real Road Atlanta race 2 of 2026-10-27 (session
michelin-raceway-road-atlanta--racing-team-turkey-by-tf-2026-34-lm--offline, track
length 4,077.33 m): battle_laps crossings (scoring-clock seconds, unwrapped lap index)
of laps.id 8195 (lap 3, the contact's first touch at 2,071 m, the spin's heavier knocks at 2,083 m),
8199 (lap 7, hit 1098 at 3,508 m in T10A), 8201/8202 (laps 9/10, hit 2883 at
384 m, across S/F), and the lap 0/1 lap_distance channel (8192/8193) at the start. The
live trace is 5 Hz scoring samples (t, unwrapped distance, km/h, in pits), as
FieldTrace.feed stores them, laid between those real crossings."""
import dataclasses
import itertools

import numpy as np
import pytest

from simcoach.battle import (
    CarLap,
    FieldTrace,
    _speed_at,
    _time_at,
    car_laps,
    contact_nearest,
    contact_touch_dist,
    live_contact_nearest,
)
from simcoach.corners import Corner, CornerMap
from simcoach.lmu.live import FieldSnapshot, VehicleState, empty_snapshot
from tests.test_live_lap import _ctx, _Source, _store, _worker

L = 4077.33
T1 = Corner(1, "Turn 1", "right", 449.4, 500.0, 572.0, label="1")


def _pts(*crossings, hz=5.0):
    """5 Hz samples at constant speed between consecutive (t, unwrapped s) crossings."""
    out = []
    for (t0, s0), (t1, s1) in itertools.pairwise(crossings):
        v = (s1 - s0) / (t1 - t0)
        out += [(t, s0 + v * (t - t0), v * 3.6, False) for t in np.arange(t0, t1, 1 / hz)]
    return out


def _car(driver, pos, *crossings):
    return {"driver": driver, "car": driver, "class_position": pos, "pts": _pts(*crossings)}


def _at(k, d):
    return k * L + d


def test_the_lap_in_progress_names_the_nearest_car_at_the_hit():
    # Lap 7 (k=6), T10A entry 3,423.7 m / exit 3,542.2 m. Committed, this same contact
    # read "Dan Harper (P19 in class), 0.4 s ahead"; live, it read nothing.
    trace = {"player_id": 0, "track_len": L, "cars": {
        0: _car("Jayson G", 25, (706.5586, _at(6, 3423.7)), (710.9006, _at(6, 3542.2))),
        33: _car("Dan Harper", 19, (706.1822, _at(6, 3423.7)), (710.5385, _at(6, 3542.2))),
        41: _car("Richard Lietz", 21, (705.9382, _at(6, 3423.7)), (710.1501, _at(6, 3542.2))),
        42: _car("Riccardo Agostini", 23, (707.2330, _at(6, 3423.7)),
                 (711.7539, _at(6, 3542.2)))}}
    line = live_contact_nearest(trace, 7, 3508.0)
    assert line == ("Nearest same-class car at the first touch of the contact (3,508 m; "
                    "other classes aren't tracked): Dan Harper (P19 in class), 0.4 s ahead -- the nearest "
                    "car, not a measured contact partner (who made contact is not measured).")


def test_the_lap_in_progress_reads_across_the_s_f_line():
    # Lap 10 (k=9) hit at 384 m, between T12's exit (3,962.9 m, lap 9) and T1's entry
    # (449.4 m, lap 10): the stored crossings can't bracket it, the live trace does.
    def car(name, pos, t_exit, t_entry):
        return _car(name, pos, (t_exit, _at(8, 3962.9)), (t_entry, _at(9, 449.4)))
    trace = {"player_id": 0, "track_len": L, "cars": {
        0: car("Jayson G", 24, 896.0341, 905.7922),
        28: car("Takeshi Kimura", 23, 895.8215, 905.5737),
        25: car("Augusto Farfus", 22, 895.4633, 905.0629),
        29: car("Finn Gehrsitz", 19, 894.6620, 904.2582)}}
    # Farfus (0.7 s ahead) and Gehrsitz (~1.5 s) are outside the 0.6 s window.
    assert live_contact_nearest(trace, 10, 384.0) == (
        "Nearest same-class car at the first touch of the contact (384 m; other classes "
        "aren't tracked): Takeshi Kimura (P23 in class), 0.2 s ahead -- the nearest car, not a "
        "measured contact partner (who made contact is not measured).")
    assert live_contact_nearest(trace, 9, 384.0) is None   # the driver's lap 9: not there


ME_LAP7 = _car("Jayson G", 25, (706.5586, _at(6, 3423.7)), (710.9006, _at(6, 3542.2)))


def test_a_lapped_rival_is_paired_on_his_own_lap():
    # A car a lap down (his k=5) through T10A beside the driver (k=6): paired by the
    # crossing nearest in time, not by the driver's lap index -- he is named.
    lapped = _car("Lapped Car", 30, (706.3586, _at(5, 3423.7)), (710.7006, _at(5, 3542.2)))
    line = live_contact_nearest({"player_id": 0, "track_len": L,
                                 "cars": {0: ME_LAP7, 50: lapped}}, 7, 3508.0)
    assert "Lapped Car (P30 in class), 0.2 s ahead" in line


def test_a_long_gap_between_samples_is_not_bridged():
    # A rival whose samples are 40.8 s apart (a lap-count lag): the straight line
    # between them would put him level with the driver at 3,508 m. Not measured there.
    gap = {"driver": "Not There", "car": "E9", "class_position": 9,
           "pts": [(686.1, _at(6, 0.0), 0.0, False), (726.9, _at(7, 2000.0), 0.0, False)]}
    assert live_contact_nearest({"player_id": 0, "track_len": L,
                                 "cars": {0: ME_LAP7, 9: gap}}, 7, 3508.0) is None


def _vs(cid, laps, dist, *, player=False):
    return VehicleState(
        id=cid, place=cid + 1, class_position=cid + 1, driver=f"D{cid}", car=f"E{cid}",
        cls="GT3", lap_dist=dist, speed=50.0, sector=0, laps=laps, in_pits=False,
        is_player=player, best_lap=None, last_lap=None, best_s1=None, best_s2=None,
        gap_leader=None, laps_behind_leader=0, gap_next=None, laps_behind_next=0)


def _snap(cars):
    s = dataclasses.replace(empty_snapshot().session, live=True, phase="race", track_len=L)
    return FieldSnapshot(session=s, cars=cars)


def test_race_lap_1_is_traced_after_the_formation_lap_reads_a_lap_ahead():
    # Real 8192 -> 8193: the formation lap ends on the grid at 3,777 m on lap 0, then the
    # start reads -302.7 m, still lap 0, and runs on from there. Every later frame read
    # backwards from 3,777 m and was dropped, so race lap 1 timed only T12 (#250).
    tr = FieldTrace()
    tr.feed(101.8, _snap([_vs(0, 0, 3777.45, player=True)]))
    for i in range(100):                         # 5 Hz, 50 m/s, from the grid past T1
        tr.feed(102.0 + i / 5, _snap([_vs(0, 0, -302.69 + 10.0 * i, player=True)]))
    laps = car_laps(tr.take(), [T1], 1)
    assert 1 in laps[0].entry                    # T1 of race lap 1 is timed


def test_a_car_reversing_after_a_spin_keeps_its_trace():
    # Real lap 3 (8195): the spin at T7 rolled the car back 2,091 m -> 2,077 m over ~14 s.
    # Only a lap-sized step back restarts a trace; this one keeps its earlier samples.
    tr = FieldTrace()
    path = [(320.0, 2000.0), (326.4, 2090.0), (328.0, 2091.0)] + [
        (328.0 + i, 2091.0 - i) for i in range(1, 15)] + [(344.1, 2083.0), (346.6, 2123.0)]
    for t, d in path:
        tr.feed(t, _snap([_vs(0, 2, d, player=True)]))
    pts = tr.take()["cars"][0]["pts"]
    assert pts[0][1] == _at(2, 2000.0) and pts[-1][1] == _at(2, 2123.0)


def test_get_stint_events_this_lap_so_far_carries_the_nearest_car(tmp_path, synthetic_lap):
    # The live path end to end: "who hit me?" just now is the lap's NEWEST contact,
    # read at its first touch -- not an earlier, heavier wall hit on the same lap, and
    # not the contact's heavier second impact. THIS LAP SO FAR carries #238's line.
    lap = synthetic_lap
    n = len(lap.channels["lap_distance"])
    ch = dict(lap.channels)
    et, mag = np.full(n, 10.0), np.full(n, 20.0)
    for at_m, t, m in ((900.0, 148.0, 2000.0),           # an unrelated wall hit
                       (1000.0, 150.0, 300.0),           # the newest contact's touch...
                       (1015.0, 150.3, 1098.0)):         # ...and its heavy impact
        i = int(np.searchsorted(ch["lap_distance"], at_m))
        et[i:], mag[i:] = t, m
    hit = int(np.searchsorted(ch["lap_distance"], 1000.0))
    ch.update({
        "elapsed_time": np.linspace(100.0, 192.5, n), "lap_start_et": np.full(n, 100.0),
        "last_impact_et": et, "last_impact_mag": mag,
        "last_impact_x": np.full(n, -1.08), "last_impact_z": np.full(n, -1.83),
        "dent_max": np.zeros(n), "dent_total": np.zeros(n),
        "detached": np.zeros(n), "overheating": np.zeros(n)})
    store = _store(tmp_path)
    live = _worker(store)
    live.source = src = _Source(lap_no=5)                # mTotalLaps 4 + 1
    src.drive(dataclasses.replace(lap, channels=ch), 1500)
    hit_m = float(ch["lap_distance"][hit])
    src._trace = tr = FieldTrace()
    for i in range(60):                                  # rival 0.3 s behind, 50 m/s
        t = 140.0 + i / 5
        tr.feed(t, _snap([_vs(0, 4, 800.0 + 50.0 * (t - 140.0), player=True),
                          _vs(7, 4, 800.0 + 50.0 * (t - 140.3))]))
    ctx = _ctx(store, on_track=True, live=live, cmap=CornerMap.load(
        "simcoach/tracks/test_track.json"))
    out = ctx.dispatch("get_stint_events", {})
    block = out[out.index("THIS LAP SO FAR"):]
    assert "impact 1098" in block
    assert (f"Nearest same-class car at the first touch of the contact ({hit_m:,.0f} m; "
            "other classes aren't tracked): D7 (P8 in class), 0.3 s behind") in block


# Real race 2 lap 3 (laps.id 8195): its first damage_events and the battle_laps T6/T7
# crossings (entry, exit, lap index) of the driver and the two cars alongside.
T6 = Corner(6, "Turn 6", "right", 1895.7, 1980.0, 2059.1, label="6")
T7 = Corner(7, "Turn 7", "left", 2075.4, 2150.0, 2230.7, label="7")
LAP3_HITS = [
    {"et": 324.28, "magnitude": 20.12, "lap_distance": 2071.1, "label": "rear-left"},
    {"et": 324.36, "magnitude": 27.14, "lap_distance": 2071.1, "label": "rear-left"},
    {"et": 325.32, "magnitude": 519.95, "lap_distance": 2082.95, "label": "rear-left"},
    {"et": 325.34, "magnitude": 684.74, "lap_distance": 2082.95, "label": "rear-left"}]


def _stored(cid, driver, pos, t6, t7, player=False):
    return CarLap(cid, driver, driver, pos, player, entry={6: t6[0], 7: t7[0]},
                  exit={6: t6[1], 7: t7[1]}, lap={6: 2, 7: 2})


def test_the_line_is_read_at_the_contacts_first_touch_not_its_heaviest():
    # The driver's call (#250): at the 2,071 m touch two cars were alongside; at the
    # spin's heaviest knock (685 at 2,083 m) a second later nobody was within 0.6 s.
    field = [_stored(0, "Jayson G", 25, (318.4409, 323.4108), (324.5242, 350.7663), True),
             _stored(20, "Matt Bell", 5, (319.0712, 323.4160), (323.8866, 329.3098)),
             _stored(27, "Francesco Castellacci", 6, (319.7669, 323.8349),
                     (324.2341, 329.8221))]
    # One contact (format.impact_groups): 2,083 m is within 30 m of the 2,071 m touch.
    assert contact_touch_dist(LAP3_HITS, newest=False) == 2071.1
    assert contact_nearest(field, [T6, T7], contact_touch_dist(LAP3_HITS, newest=False)) == (
        "Nearest same-class car at the first touch of the contact (2,071 m; other classes "
        "aren't tracked): Francesco Castellacci (P6 in class), 0.1 s ahead; Matt Bell (P5 in "
        "class), 0.5 s ahead -- the nearest car, not a measured contact partner (who made "
        "contact is not measured).")


def test_speed_is_not_interpolated_across_a_long_sample_gap():
    pts = [(10.0, 100.0, 180.0), (13.0, 250.0, 180.0)]        # 3 s apart
    assert _speed_at(pts, 175.0) is None and _time_at(pts, 175.0) is None


def test_a_dropped_s_f_mismatch_frame_still_interpolates():
    # FieldTrace.feed drops the one frame where lap count and distance disagree at S/F:
    # that leaves a 0.4 s gap in an otherwise 5 Hz trace, which is still measured.
    pts = [(10.0, 990.0, 180.0), (10.2, 1000.0, 180.0), (10.6, 1020.0, 200.0),
           (10.8, 1030.0, 200.0)]
    assert _time_at(pts, 1010.0) == pytest.approx(10.4)
    assert _speed_at(pts, 1010.0) == pytest.approx(190.0)


def _hits(*rows):
    return [{"et": t, "lap_distance": d, "magnitude": m, "label": "front-left"}
            for t, d, m in rows]


def test_a_contact_chains_each_impact_to_the_previous_one():
    # Real laps.id 7988: one contact from 1,273.5 m to 1,305.1 m (31.6 m end to end,
    # each step <= 14 m). Its first touch is 1,273.5 m, never the trailing 99 graze.
    real_7988 = _hits((141.72, 1273.5, 6854), (142.00, 1287.1, 1889),
                      (142.26, 1293.4, 305), (142.60, 1305.1, 99))
    assert contact_touch_dist(real_7988, newest=True) == 1273.5
    assert contact_touch_dist(real_7988, newest=False) == 1273.5


def test_a_long_scrape_is_one_contact_from_its_first_impact():
    # Real laps.id 7688: a scrape 699.3 -> 757.1 m over 5.1 s, 11 impacts.
    real_7688 = _hits((904.56, 699.3, 1112), (904.66, 703.8, 1467), (905.38, 716.1, 787),
                      (905.60, 722.3, 877), (905.90, 724.6, 832), (906.34, 728.1, 919),
                      (906.62, 731.7, 568), (906.84, 733.5, 666), (908.78, 746.6, 1051),
                      (909.66, 757.1, 430), (909.68, 757.1, 623))
    assert contact_touch_dist(real_7688, newest=True) == 699.3


def test_an_unplaced_impact_does_not_split_a_contact():
    hits = _hits((10.0, 1000.0, 300), (12.0, float("nan"), 900), (14.0, 1005.0, 200))
    assert contact_touch_dist(hits, newest=True) == 1000.0


def test_a_fast_scrape_chains_by_time():
    # 60 m/s along a wall: 36 m between impacts 0.6 s apart -- still one contact.
    hits = _hits((10.0, 1000.0, 300), (10.6, 1036.0, 400), (11.2, 1072.0, 500))
    assert contact_touch_dist(hits, newest=True) == 1000.0
