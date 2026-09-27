import keyring as _keyring
import numpy as np
import pytest
from keyring.backend import KeyringBackend

from simcoach.model_types import LapRecord

_G = 9.80665

# The synthetic circuit: 3000 m, two mapped corners (simcoach/tracks/test_track.json).
# Corner 1 (800-1200, apex 1000) is a slow LEFT hairpin; corner 2 (2000-2400, apex
# 2200) is a faster RIGHT sweeper.
#
# They differ in EVERY input a driver controls -- approach speed, brake pressure,
# brake-zone length, where the speed minimum falls relative to the MAPPED apex,
# steering lock, lateral load, throttle pick-up rate, ABS/TC engagement, and how
# much track is used on exit. That is deliberate (#70): the two corners used to be
# mirror images -- same brake, same |steer|, same speed dip, opposite sign -- and 13
# of CornerMetrics' 17 numeric fields came out IDENTICAL, so every "corner 1 differs
# from corner 2" / "it read the right corner" assertion on this fixture passed by
# construction rather than because the code was right. Keep them distinguishable:
# tests/test_analysis.py::test_corner_metrics_fields_vary_or_are_allowlisted walks
# the dataclass by reflection and fails on any field that ties.
#
# Speed is not hand-drawn. It is a quasi-steady profile -- brake to each minimum at
# that corner's decel limit, accelerate away at its traction limit, capped by the
# straight-line speed the car can reach -- so brake, throttle, long_g and the time
# axis are all consequences of one speed trace instead of three inventions that
# happen to sit side by side. Braking therefore precedes each minimum, the minimum
# is the minimum, and throttle comes after it.
_MU = 1.35   # pedal scale: brake input = required deceleration (g) / _MU

# (min_dist, min_speed m/s, coast g, braking g, exit accel g, throttle ramp m,
#  brake-release dist). Speeds here are m/s because the profile below is physics; the
# stored `speed` channel is km/h, the unit capture writes. min_dist is where the
# DRIVER's minimum falls -- late for the hairpin, early for the sweeper -- not the
# mapped apex, so apex_spd and min_spd are two readings rather than one number twice.
#
# The release distance is the two corners' difference in TECHNIQUE, not just in pedal
# numbers: the hairpin is trail-braked at full pressure all the way to its minimum
# (release == min_dist), while the sweeper is braked in a straight line, released at
# 2140 before turn-in, and the last 1.5 m/s bleeds off coasting at 0.09 g -- under the
# 0.1 gate, so it is not braking. Braked the same way, the two corners tie on
# trailbrake_overlap however different everything else about them is.
_CORNERS = (
    (1015.0, 14.0, 1.30, 1.30, 0.55, 120.0, 1015.0),   # 1: slow left hairpin
    (2185.0, 26.0, 0.09, 0.95, 0.40, 60.0, 2140.0),    # 2: faster right sweeper
)
# Straight-line speed cap (m/s): the short run to the hairpin tops out lower than the
# long one to the sweeper, and an unmapped constriction near 2800 brings the car back
# to the speed it crosses the line at. Different caps are what make the two corners'
# ENTRY speeds differ -- a single top speed ties entry_spd no matter how the corners
# themselves are driven.
# (the caps also set the lap time: the integrated time axis ends at 92.2 s, within
# 0.3 s of the record's nominal 92.5 s. Nothing reads `time`; it is there to agree.)
_CAP = ((0.0, 31.0), (900.0, 31.0), (1750.0, 36.5), (2600.0, 36.5), (2800.0, 31.0),
        (3000.0, 31.0))
#
# SIGN CONVENTIONS -- LMU's, measured on real stored laps (#70), not assumed:
#   steering      negative = left (RIGHT_IS_POSITIVE)
#   lat_g         POSITIVE in a LEFT corner, negative in a right (ribbon.py; real left
#                 corners read +0.6..+1.4 g) -- the OPPOSITE sign to steering
#   long_g        POSITIVE under BRAKING, negative accelerating (real: +1.3..+1.7 g
#                 at brake > 0.5, -0.2..-0.5 g at full throttle)
#   path_lateral  positive = car RIGHT of the centre path
#   track_edge    signed, always the SAME sign as path_lateral (the edge on the side
#                 the car is on; 100% agreement where |path_lateral| > 1 m)
# The fixture once had lat_g and long_g the other way round, and every consumer that
# takes np.abs() of them could not notice -- only the one reading the sign could.
#
# Steering: peak lock at the driver's minimum, released through the exit.
_STEER = (
    ((860.0, 980.0, 1015.0, 1120.0, 1220.0), (0.0, -0.30, -0.62, -0.30, 0.0)),
    ((2100.0, 2125.0, 2185.0, 2320.0, 2430.0), (0.0, 0.10, 0.38, 0.20, 0.0)),
)
# Lateral g per unit of steering lock, NEGATIVE because lat_g's sign is opposite to
# steering's. The sweeper pulls MORE g on LESS lock, which is what distinguishes a fast
# corner from a hairpin -- and is why lat_g is not one constant multiple of `steer`
# (that tied peak_lat_g).
_LAT_PER_STEER = (-2.02, -4.03)
# The racing line, as path_lateral (m, + = right of centre): outside-inside-outside.
# The left hairpin enters wide RIGHT, clips the apex on the LEFT, and runs out to the
# right-hand exit kerb (0.3 m from it -- the corner's closest approach); the right
# sweeper is the mirror, and never gets closer than 1.5 m (its left exit kerb).
_PATH_LAT = (
    ((860.0, 940.0, 1015.0, 1120.0, 1220.0), (0.0, 3.8, -4.2, 4.7, 0.0)),
    ((2040.0, 2120.0, 2185.0, 2320.0, 2430.0), (0.0, -4.0, 4.5, -5.0, 0.0)),
)
_HALF_WIDTH = ((800.0, 1230.0, 5.0), (2000.0, 2440.0, 6.5))   # (start, end, m)
# ABS only where the brake is genuinely hard (real laps: ABS below brake 0.1 is ~1% of
# samples): most of the hairpin's 0.97 stop, a short bite in the sweeper's 0.70 zone.
_ABS_ON = ((990.0, 1015.0), (2112.0, 2120.0))
_TC_ON = ((1015.0, 1115.0), (2185.0, 2215.0))   # hairpin exit spins up; sweeper barely


def _span(dist, spans, value=1.0):
    out = np.zeros(len(dist))
    for lo, hi in spans:
        out[(dist >= lo) & (dist <= hi)] = value
    return out


@pytest.fixture
def synthetic_lap():
    n = 1000
    dist = np.linspace(0, 3000, n)

    # `v` is m/s -- the units the physics above is written in. The stored `speed`
    # channel is km/h, because that is what capture writes (smm.py: |mLocalVel| * 3.6)
    # and what format.py converts to mph from.
    v = np.interp(dist, [d for d, _ in _CAP], [s for _, s in _CAP])
    for md, mv, cg, bg, ag, _, rel in _CORNERS:
        v_rel = np.sqrt(mv**2 + 2 * cg * _G * (md - rel))     # speed at brake release
        coast = np.sqrt(mv**2 + 2 * cg * _G * np.clip(md - dist, 0, None))
        braking = np.sqrt(v_rel**2 + 2 * bg * _G * np.clip(rel - dist, 0, None))
        exiting = np.sqrt(mv**2 + 2 * ag * _G * np.clip(dist - md, 0, None))
        v = np.minimum(v, np.select(
            [dist < rel, dist <= md], [braking, coast], default=exiting))

    long_g = -v * np.gradient(v, dist) / _G   # LMU sign: + decelerating, - accelerating
    brake = np.clip(long_g / _MU, 0.0, 1.0)
    throttle = np.ones(n)
    for md, _, _, _, _, ramp, rel in _CORNERS:
        # From brake release (not from the minimum): the clip holds throttle at 0
        # through the coast too, so the car is never shown at full throttle in the
        # one sample between the brake coming off and the minimum.
        seg = (dist >= rel) & (dist <= md + ramp)
        throttle = np.where(seg, np.clip((dist - md) / ramp, 0.0, 1.0), throttle)
    throttle = np.where(brake > 0.02, 0.0, throttle)   # off the gas whenever braking

    steer = np.zeros(n)
    lat_g = np.zeros(n)
    path_lat = np.zeros(n)
    for (sd, sv), k, (pd, pv) in zip(_STEER, _LAT_PER_STEER, _PATH_LAT):
        s = np.interp(dist, sd, sv, left=0.0, right=0.0)
        steer += s
        lat_g += s * k
        path_lat += np.interp(dist, pd, pv, left=0.0, right=0.0)
    track_edge = np.full(n, 7.0)
    for lo, hi, w in _HALF_WIDTH:
        track_edge[(dist >= lo) & (dist <= hi)] = w
    track_edge = np.copysign(track_edge, path_lat)   # the edge on the car's own side

    # Elapsed time integrated from the speed trace, so the time axis agrees with it.
    time = np.concatenate(([0.0], np.cumsum(np.diff(dist) / ((v[:-1] + v[1:]) / 2))))

    ch = {
        "lap_distance": dist,
        "time": time,
        "speed": v * 3.6,
        "throttle": throttle,
        "brake": brake,
        "steering": steer,
        "lat_g": lat_g,
        "long_g": long_g,
        "gear": np.clip(np.round(v / 6.5), 1.0, 6.0),
        "abs_active": _span(dist, _ABS_ON),
        "tc_active": _span(dist, _TC_ON),
        "track_edge": track_edge,
        "path_lateral": path_lat,
    }
    warm = np.linspace(78.0, 86.0, n)                  # tires coming up to temperature
    for t in ("fl", "fr", "rl", "rr"):
        ch[f"tire_temp_{t}"] = warm + 4.0 * (np.abs(steer) > 0.1)
        ch[f"tire_press_{t}"] = np.full(n, 27.5)
        ch[f"brake_temp_{t}"] = 200 + 150 * (brake > 0.1)   # hotter under braking
        # mGripFract as the game actually writes it: flat 0.0, always (#43, rig-
        # confirmed over 173 laps). The fixture used to invent a channel that dipped
        # under braking, which let a test read a "grip" signal the product never has.
        ch[f"grip_{t}"] = np.zeros(n)
        ch[f"tire_wear_{t}"] = np.linspace(1.0, 0.97, n)   # slight wear over the lap
    ch["fuel"] = np.linspace(80.0, 77.4, n)   # liters; 2.6 L consumed over the lap
    return LapRecord("lmu", "test_track", "test_car", 92.5, True, 0.0, ch)


class MemoryKeyring(KeyringBackend):
    priority = 1
    def __init__(self): self._store = {}
    def get_password(self, service, username): return self._store.get((service, username))
    def set_password(self, service, username, password): self._store[(service, username)] = password
    def delete_password(self, service, username): self._store.pop((service, username), None)


@pytest.fixture(autouse=True)
def _no_live_game(monkeypatch):
    """The suite must not depend on whether LMU is running on the dev box.

    Rig 2026-09-15: with LMU open, five chat-history tests failed because the
    server read the LIVE session id from shared memory and (then, #97) refused
    to thread a stored chat from another session (#167 now labels such turns
    instead, which a live id would still change). Tests that want a
    live game stub these themselves; everyone else sees no game."""
    from simcoach.lmu import smm
    for name in ("read_live_status", "read_session_track_car", "read_live_session_id"):
        if hasattr(smm, name):
            monkeypatch.setattr(smm, name, lambda: None)
    # The suite's own test-named mappings have a live writer (the test itself), so
    # the #147 "is the LMU process running" guard must not depend on the dev box.
    monkeypatch.setattr(smm, "_lmu_alive", lambda: True, raising=False)


@pytest.fixture(autouse=True)
def _isolated_car_lookup(monkeypatch):
    """The suite must not read the driver's real LMU Results dir (#179): car_lookup's
    entry-table cache would scan however many thousand real result XMLs live there,
    and a timing-sensitive test would pass or fail on the OS file cache instead of the
    code under test (a real, reproduced failure -- see #176's
    test_events_laps_frame_is_not_delayed_by_a_slow_optimal_compute). Point
    car_lookup.results_dir at nothing and reset its caches for every test; a test that
    deliberately wants the real dir overrides `car_lookup.results_dir` itself (e.g.
    `monkeypatch.setattr(car_lookup, "results_dir", install.results_dir)`).

    The once-a-second dir check (#201) is off (interval 0): a test that changes the dir
    and looks again must see it now. The #201 tests set the interval back themselves."""
    from simcoach import car_lookup
    monkeypatch.setattr(car_lookup, "results_dir", lambda: None)
    monkeypatch.setattr(car_lookup, "_CHECK_EVERY_S", 0.0)
    car_lookup._cache["sig"] = None
    car_lookup._cache["table"] = {}
    car_lookup._cache["checked"] = None
    car_lookup._file_cache.clear()
    car_lookup._warm_started = False
    car_lookup._warmed.clear()


@pytest.fixture
def mem_keyring():
    prev = _keyring.get_keyring()
    kr = MemoryKeyring()
    _keyring.set_keyring(kr)
    yield kr
    _keyring.set_keyring(prev)


def with_map_rev(client, url, body):
    # PUT .../map and POST .../rotation require the rev of the map they edit (#166): fetch
    # it as the editor does, from the map GET beside the write route. A non-dict body (a
    # shape test) or one that already names a rev goes out as given.
    if not isinstance(body, dict) or "rev" in body:
        return body
    rev = client.get(url.rsplit("/", 1)[0] + "/map").json().get("rev")
    return body if rev is None else {**body, "rev": rev}
