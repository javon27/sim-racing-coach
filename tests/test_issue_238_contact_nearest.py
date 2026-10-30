"""#238: a contact line names the nearest same-class car(s) at that spot from the stored
field trace -- "nearest car", never "who hit you" (contact attribution isn't measured).

Fixtures are the real battle_laps crossings of Bahrain Outer race lap 4 (laps.id 8078,
2026-10-21), trimmed to T3-T5: scoring-clock seconds at each corner's entry/exit, and
the car's unwrapped lap index each corner was timed on. The contact's first impact was
at 1,552.1 m and its heaviest at 1,578.7 m (T4); the result log named Niklas Weissel.
#250: the line is read at the contact's FIRST touch."""
import dataclasses

import numpy as np
import pytest

from simcoach.battle import CarLap, contact_nearest, contact_touch_dist
from simcoach.coach_tools import CoachContext
from simcoach.corners import Corner, CornerMap
from simcoach.store import LapStore

T3 = Corner(3, "Turn 3", "right", 900.0, 942.2, 1019.6, label="3")
T4 = Corner(4, "Turn 4", "right", 1441.5, 1518.8, 1585.6, label="4")
T5 = Corner(5, "Turn 5", "left", 1589.1, 1652.4, 1729.7, label="5")
CORNERS = [T3, T4, T5]


def _car(cid, driver, pos, entry, exit_, lap=None, player=False):
    lap = lap or {n: 3 for n in entry}
    return CarLap(cid, driver, f"E{cid}", pos, player, entry=dict(entry), exit=dict(exit_),
                  lap=dict(lap))


ME = _car(15, "Jayson G", 13, {3: 381.0718, 4: 391.3173, 5: 396.4046},
          {3: 383.7931, 4: 396.2646, 5: 408.0403}, player=True)
WEISSEL = _car(9, "Niklas Weissel", 7, {3: 381.2451, 4: 391.4310, 5: 396.5480},
               {3: 383.9412, 4: 396.4145, 5: 400.5474})
RUPP = _car(13, "Johannes Rupp", 6, {3: 380.4049, 4: 390.5793, 5: 395.3189},
            {3: 383.0588, 4: 395.2046, 5: 398.9650})
# Through the same corner, 5 s up the road: near in distance, far in time.
MANAUS = _car(2, "Manaus SimRacing", 4, {3: 376.1115, 4: 386.4652, 5: 391.3362},
              {3: 378.8561, 4: 391.2273, 5: 394.7881})
FIELD = [ME, WEISSEL, RUPP, MANAUS]


# The lap's real damage_events (laps.id 8078), in the order hit.
HITS = [{"et": 395.18, "magnitude": 202.0, "lap_distance": 1552.13, "label": "rear-right"},
        {"et": 395.28, "magnitude": 319.0, "lap_distance": 1557.2, "label": "rear-right"},
        {"et": 395.38, "magnitude": 458.0, "lap_distance": 1557.2, "label": "rear-right"},
        {"et": 395.78, "magnitude": 375.0, "lap_distance": 1567.8, "label": "rear-right"},
        {"et": 396.04, "magnitude": 668.0, "lap_distance": 1578.68, "label": "rear-right"}]


def test_names_weissel_nearest_at_the_real_lap_4_contact():
    line = contact_nearest(FIELD, CORNERS, contact_touch_dist(HITS, newest=False))
    # All five impacts are one contact (within format._IMPACT_GROUP_M of 1,552 m).
    assert line.startswith("Nearest same-class car at the first touch of the contact "
                           "(1,552 m; other classes aren't tracked): Niklas Weissel (P7 in class), 0.1 s behind")
    assert "Manaus" not in line             # same corner, 5 s ahead: not near
    assert "Rupp" not in line               # 0.98 s ahead here: outside the window
    assert "hit you" not in line and "nearest" in line.lower()
    assert "not measured" in line


def test_two_cars_inside_the_window_are_both_named_closest_first():
    # A second car 0.5 s ahead of the driver all through T4: inside +/-0.6 s.
    close = _car(20, "Close Second", 5, {4: 390.8173}, {4: 395.7646})
    line = contact_nearest(FIELD + [close], CORNERS, 1552.13)
    assert line.index("Niklas Weissel") < line.index("Close Second (P5 in class), 0.5 s ahead")


def test_the_window_is_0_6_s():
    # #238 review: true contact partners sat 0.00-0.58 s away over 52 real laps; at 1.0 s
    # wall hits still named a car. Rupp is 0.98 s ahead at 1,552 m: not named.
    assert "Rupp" not in contact_nearest(FIELD, CORNERS, 1552.13)
    at_065 = _car(21, "Just Out", 5, {4: 390.6673}, {4: 395.6146})     # 0.65 s ahead
    assert contact_nearest([ME, at_065], CORNERS, 1552.13) is None


def test_a_rival_in_the_pit_lane_at_that_moment_is_not_named():
    pitting = dataclasses.replace(WEISSEL, pit=[(394.0, 420.0)])    # in the lane at 395.3
    assert contact_nearest([ME, pitting], CORNERS, 1552.13) is None
    earlier = dataclasses.replace(WEISSEL, pit=[(300.0, 340.0)])    # a stop laps before
    assert "Niklas Weissel" in contact_nearest([ME, earlier], CORNERS, 1552.13)


def test_a_bracket_across_two_of_a_rivals_laps_is_not_interpolated():
    # T3 on his previous lap, T4 on this one (the seam pairing car_laps makes for a car
    # crossing the line around the driver). Read as one lap, 1,300 m would put him
    # level with the driver; it is not one lap, so he is not timed there.
    seam = _car(6, "Jesus Orozco", 15, {3: 381.3, 4: 391.4}, {3: 384.0, 4: 396.3},
                lap={3: 2, 4: 3})
    assert contact_nearest([ME, seam], CORNERS, 1300.0) is None
    assert "Orozco" in contact_nearest([ME, seam], CORNERS, 1500.0)


def test_past_the_last_timed_corner_says_nothing():
    # 3,400 m: after the last corner, before S/F -- the trace stops at T5's exit here.
    assert contact_nearest(FIELD, CORNERS, 3400.0) is None
    assert contact_nearest(FIELD, CORNERS, 500.0) is None     # before the first


def test_missing_field_or_untimed_player_says_nothing():
    assert contact_nearest([], CORNERS, 1578.68) is None
    assert contact_nearest([WEISSEL, RUPP], CORNERS, 1578.68) is None    # no player row
    untimed = dataclasses.replace(ME, entry={}, exit={}, lap={})
    assert contact_nearest([untimed, WEISSEL], CORNERS, 1578.68) is None
    assert contact_nearest([ME, MANAUS], CORNERS, 1578.68) is None       # nobody near


@pytest.fixture
def contact_ctx(tmp_path, synthetic_lap):
    """One race lap on test_track, every hit between T1's exit (1,200 m) and T2's entry
    (2,000 m): a light tap at 1,240 m, then a contact that touches at 1,690 m and lands
    its heavy hit at 1,700 m, then another light tap at 1,950 m. #250: a stored lap
    reads the heaviest CONTACT (impact group) at its FIRST touch -- 1,690 m."""
    ch = dict(synthetic_lap.channels)
    d = np.asarray(ch["lap_distance"], float)
    n = len(d)
    for key in ("last_impact_et", "last_impact_mag", "last_impact_x", "last_impact_z",
                "dent_max", "dent_total", "overheating", "detached"):
        ch[key] = np.zeros(n)
    ch["last_impact_x"][:] = -1.08
    ch["last_impact_z"][:] = -1.83
    for at_m, et, mag in ((1240.0, 2178.5, 365.0), (1690.0, 2189.8, 300.0),
                          (1700.0, 2190.0, 1200.0), (1950.0, 2196.0, 200.0)):
        i = int(np.argmin(np.abs(d - at_m)))
        ch["last_impact_et"][i:], ch["last_impact_mag"][i:] = et, mag
    idx = int(np.argmin(np.abs(d - 1690.0)))
    store = LapStore(str(tmp_path / "db.sqlite"), str(tmp_path / "telem"))
    lap = dataclasses.replace(synthetic_lap, channels=ch, session_id="s1--online",
                              phase="race", lap_time=90.0, timestamp=90.0)
    lap_id = store.save(lap)
    ctx = CoachContext(store, cmap=CornerMap.load("simcoach/tracks/test_track.json"),
                       track=lap.track, car=lap.car, session_id="s1--online", on_track=True)
    return ctx, store, lap_id, d[idx]


def test_get_stint_events_attaches_the_nearest_car_to_the_contact_line(contact_ctx):
    ctx, store, lap_id, hit = contact_ctx
    # 1,200 m -> 2,000 m in 10 s: the driver reaches the hit 0.04 s/m * (hit - 1200)
    # after 100.0; the rival is 0.3 s behind all the way along.
    me = _car(1, "Me", 5, {1: 90.0, 2: 110.0}, {1: 100.0, 2: 115.0}, player=True)
    rival = _car(2, "Rival Driver", 4, {1: 90.3, 2: 110.3}, {1: 100.3, 2: 115.3})
    store.save_battle(lap_id, [me, rival])
    out = ctx.dispatch("get_stint_events", {})
    line = next(x for x in out.splitlines() if "Contact:" in x)
    nearest = out.splitlines()[out.splitlines().index(line) + 1]
    assert nearest.strip() == (
        f"Nearest same-class car at the first touch of the contact ({hit:,.0f} m; "
        "other classes aren't tracked): Rival Driver (P4 in class), 0.3 s behind -- the nearest car, "
        "not a measured contact "
        "partner (who made contact is not measured).")


def test_get_stint_events_without_a_stored_field_adds_nothing(contact_ctx):
    ctx, _, _, _ = contact_ctx
    out = ctx.dispatch("get_stint_events", {})
    assert "Contact:" in out
    assert "Nearest" not in out
