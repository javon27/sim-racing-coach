import numpy as np
import pytest

from simcoach.raceline import (
    CarParams,
    min_curvature_line,
    speed_profile,
    solve_corners,
    _split_run_apexes,
)


def test_split_run_apexes_prominence():
    # Two bumps with a DEEP valley between -> two apexes (a chicane).
    kv = np.array([0.0, 0.01, 0.0, 0.01, 0.0])
    assert len(_split_run_apexes(kv, min_prom=0.005)) == 2
    # Same two bumps but a SHALLOW valley (< prominence) -> merged to one corner.
    kv2 = np.array([0.0, 0.01, 0.009, 0.01, 0.0])
    assert len(_split_run_apexes(kv2, min_prom=0.005)) == 1
    # A flat plateau (constant-radius sweeper) -> one apex, centered.
    flat = np.full(9, 0.02)
    out = _split_run_apexes(flat, min_prom=0.005)
    assert out == [4]  # single apex at the plateau centre


# --- synthetic corridors (world coords, closed loop, paired by station) ---

def _straight(n=40, length=400.0, halfwidth=5.0):
    x = np.linspace(0.0, length, n, endpoint=False)
    left = np.column_stack([x, np.full(n, halfwidth)])
    right = np.column_stack([x, np.full(n, -halfwidth)])
    return left, right


def _annulus(n=180, r_in=90.0, r_out=110.0):
    """One constant-radius LEFT-hander: stations run counter-clockwise, which
    _LEFT_TURN_IS_CROSS_POSITIVE calls a left turn, so the driver's left is the
    INSIDE of the turn."""
    th = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    inner = np.column_stack([r_in * np.cos(th), r_in * np.sin(th)])
    outer = np.column_stack([r_out * np.cos(th), r_out * np.sin(th)])
    return inner, outer  # left = inner edge, right = outer edge


def _annulus_cw(n=180, r_in=90.0, r_out=110.0):
    """The same corridor driven the other way -- a constant-radius RIGHT-hander,
    so now the driver's left is the OUTSIDE."""
    inner, outer = _annulus(n, r_in, r_out)
    return outer[::-1], inner[::-1]  # left = outer edge, right = inner edge


def _stadium_edges(L=200.0, R=50.0, half_w=6.0, nb=40, na=40):
    """Closed stadium corridor: two straights joined by two semicircle ends."""
    xb = np.linspace(0.0, L, nb, endpoint=False)
    bottom = np.column_stack([xb, np.full(nb, -R)])
    thr = np.linspace(-np.pi / 2, np.pi / 2, na, endpoint=False)
    right = np.column_stack([L + R * np.cos(thr), R * np.sin(thr)])
    xt = np.linspace(L, 0.0, nb, endpoint=False)
    top = np.column_stack([xt, np.full(nb, R)])
    thl = np.linspace(np.pi / 2, 3 * np.pi / 2, na, endpoint=False)
    left_arc = np.column_stack([R * np.cos(thl), R * np.sin(thl)])
    center = np.vstack([bottom, right, top, left_arc])

    t = np.roll(center, -1, axis=0) - np.roll(center, 1, axis=0)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    normal = np.column_stack([-t[:, 1], t[:, 0]])  # left-hand unit normal
    return center + half_w * normal, center - half_w * normal, center


# --- tests ---

def test_straight_corridor_centered_and_uncapped():
    left, right = _straight()
    alpha, line = min_curvature_line(left, right)
    # interior (away from the closed-loop seam) stays on the centerline
    assert np.allclose(alpha[10:30], 0.5, atol=0.05)

    _, kappa = speed_profile(line, 1.5, 9.81, 14.0, 9.0)
    assert np.all(kappa[10:30] < 1e-6)
    # cornering cap is effectively unlimited where the line is straight
    v_cap = np.sqrt(1.5 * 9.81 / np.maximum(kappa, 1e-9))
    assert v_cap[20] > 1e3


def test_arc_hugs_inside_with_constant_corner_speed():
    left, right = _annulus()  # left = inner edge
    alpha, line = min_curvature_line(left, right, iterations=2000)
    # curve-shortening pulls the band toward the inside (left, alpha->0)
    assert alpha.mean() < 0.4

    v, kappa = speed_profile(line, 1.5, 9.81, 14.0, 9.0)
    # a full constant-radius loop -> ~constant corner speed = sqrt(mu*g*r)
    assert np.std(v) / np.mean(v) < 0.05
    r_line = 1.0 / np.mean(kappa)
    v_expected = np.sqrt(1.5 * 9.81 * r_line)
    assert abs(np.mean(v) - v_expected) < 0.1 * v_expected


def test_solve_corners_finds_the_two_arcs():
    left, right, _ = _stadium_edges()
    corners = solve_corners(left, right)
    assert len(corners) == 2

    car = CarParams()
    v_ref = np.sqrt(car.mu * car.g * 50.0)  # centerline arc speed, m/s
    n = 160  # stadium: 40 bottom + 40 right-arc + 40 top + 40 left-arc
    for c in corners:
        assert 0.5 * v_ref * 3.6 < c["min_speed_kmh"] < 1.7 * v_ref * 3.6
        assert c["start_dist"] != c["end_dist"]
        assert c["min_speed_kmh"] > 0.0
        # station indices are valid and self-consistent with the dist values
        for k in ("apex_idx", "start_idx", "end_idx"):
            assert isinstance(c[k], int) and 0 <= c[k] < n

    # the two apexes sit in the two arc index bands, not on the straights
    apex_bands = sorted(c["apex_idx"] for c in corners)
    assert 40 <= apex_bands[0] < 80    # right arc: stations 40..79
    assert 120 <= apex_bands[1] < 160  # left arc: stations 120..159


def test_solve_corners_min_speed_is_kmh_not_ms():
    """#55: pin the UNIT of the corner speed, not the conversion.

    The stadium's arc radius is 50 m BY CONSTRUCTION, so a point-mass at the
    default mu reaches sqrt(mu*g*r) = 27.1 m/s = 97.6 km/h through it (the
    min-curvature line's own radius shifts that a little, hence the band). The
    band is chosen so ONLY km/h fits it:

      * m/s (the pre-fix raceline convention)          ~25-27  -> below LO
      * km/h (correct)                                 ~92     -> inside
      * km/h converted twice (a 3.6 applied on top)    ~330    -> above HI

    So this fails if the conversion is dropped, doubled, or inverted -- unlike a
    round-trip assert, which passes under every one of those (#43-#47).
    """
    LO, HI = 60.0, 180.0
    car = CarParams()
    v_arc_ms = np.sqrt(car.mu * car.g * 50.0)          # 27.1 m/s, from the geometry
    assert v_arc_ms < LO                               # the m/s answer can't reach LO
    assert v_arc_ms * 3.6 * 3.6 > HI                   # a double conversion overshoots HI

    left, right, _ = _stadium_edges()
    for c in solve_corners(left, right):
        assert LO < c["min_speed_kmh"] < HI
    # and the unitless key is gone, so a stale reader fails loudly instead of
    # silently reading a number 3.6x off.
    assert all("min_speed" not in c for c in solve_corners(left, right))


def test_bank_favours_the_turn_it_is_cambered_for_not_the_positive_sign():
    """#63: ``bank`` is signed by which side is HIGHER (+ = the driver's LEFT is
    higher, #58), NOT by whether the camber helps. A corner cambered for cornering
    has its OUTER edge higher, and the outside of a LEFT-hander is the driver's
    right -- so a properly cambered left-hander reads NEGATIVE.

    Both handednesses are driven here deliberately: the same corridor, same bank
    MAGNITUDE, opposite travel direction. The favourable sign differs by direction,
    so one direction alone cannot distinguish "bank relative to the turn" from
    "raw positive = faster". Under the raw-sign reading the right-hander still
    passes and the left-hander fails -- which is exactly why the bug shipped.

    Asserted through ``solve_corners`` (the shipped entry point) so the
    centerline turn_sign threading is covered, not just ``speed_profile``.
    """
    theta = np.radians(15.0)
    for turn, edges, favourable in (("left", _annulus(), -theta),
                                    ("right", _annulus_cw(), +theta)):
        left, right = edges
        flat = solve_corners(left, right)
        fav = solve_corners(left, right, bank=np.full(len(left), favourable))
        adv = solve_corners(left, right, bank=np.full(len(left), -favourable))
        assert len(flat) == len(fav) == len(adv) == 1, turn
        v_flat = flat[0]["min_speed_kmh"]
        assert fav[0]["min_speed_kmh"] > v_flat, f"{turn}: camber for the turn must help"
        assert adv[0]["min_speed_kmh"] < v_flat, f"{turn}: camber against it must hurt"


def test_adverse_camber_stays_a_penalty_so_the_fix_is_not_abs():
    """The other half of #63: abs(bank) would also un-break left-handers, and be
    just as wrong -- genuine adverse camber (INSIDE edge higher) has to stay a
    penalty. Pinned on MAGNITUDE against the closed form, not just ordering, so
    the two cases have to straddle flat by the right amount rather than merely
    differ: favourable is ~1.40x flat here and adverse ~0.77x, and abs() would
    report 1.40x for both.
    """
    left, right = _annulus()                       # a LEFT-hander
    _, line = min_curvature_line(left, right, iterations=2000)
    theta = np.radians(15.0)
    turning_left = np.full(len(line), -1.0)        # spelled out, not derived here
    v_flat, kappa = speed_profile(line, 1.5, 9.81, 14.0, 9.0)
    # +theta on a left-hander = the driver's left (the INSIDE) is higher = adverse.
    v_adv, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0,
                             bank=np.full(len(line), theta), turn_sign=turning_left)
    v_fav, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0,
                             bank=np.full(len(line), -theta), turn_sign=turning_left)

    mu, g, t = 1.5, 9.81, np.tan(theta)
    r_line = 1.0 / np.mean(kappa)                  # the solved line's own radius
    expect_fav = np.sqrt(g * r_line * (mu + t) / (1.0 - mu * t))
    expect_adv = np.sqrt(g * r_line * (mu - t) / (1.0 + mu * t))
    assert expect_adv < np.mean(v_flat) < expect_fav        # the fixture separates them
    assert abs(np.mean(v_fav) - expect_fav) < 0.05 * expect_fav
    assert abs(np.mean(v_adv) - expect_adv) < 0.05 * expect_adv


def test_flat_and_near_flat_banking_leave_the_profile_alone():
    left, right = _annulus()
    _, line = min_curvature_line(left, right, iterations=2000)
    sign = np.full(len(line), -1.0)
    v_flat, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0)
    # bank=None, bank=zeros and a 0.01 deg cross-slope all read as flat
    v_zero, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0,
                              bank=np.zeros(len(line)), turn_sign=sign)
    assert np.allclose(v_zero, v_flat)
    v_tiny, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0,
                              bank=np.full(len(line), np.radians(0.01)), turn_sign=sign)
    assert np.allclose(v_tiny, v_flat, rtol=1e-3)


def test_bank_without_turn_sign_is_refused_not_guessed():
    """#63's root cause was a bank whose sign nobody had resolved against the turn.
    There is no default: a caller that forgets turn_sign must fail loudly rather
    than silently get one of the two readings (raw, or line-derived instead of the
    centerline policy solve_corners applies)."""
    left, right = _annulus()
    _, line = min_curvature_line(left, right, iterations=2000)
    with pytest.raises(ValueError, match="turn_sign"):
        speed_profile(line, 1.5, 9.81, 14.0, 9.0, bank=np.full(len(line), 0.1))


def test_a_flat_bank_needs_no_turn_sign_and_reads_as_bank_none():
    """The guard must not reject "flat, expressed as data" -- a shape this repo
    really produces (ribbon.reconstruct_ribbon zeroes bank when the capture has no
    elevation channel; its _round maps non-finite to 0.0). tan(0) * anything == 0,
    so the sign provably cannot matter and there is nothing to decide. But ONE
    non-zero station is signal, and signal still has to be signed -- otherwise the
    exemption would quietly become the missing default that #63 was."""
    left, right = _annulus()
    _, line = min_curvature_line(left, right, iterations=2000)
    v_none, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0)

    v_zeros, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0, bank=np.zeros(len(line)))
    assert np.array_equal(v_none, v_zeros)            # identical, not merely close
    # all-non-finite says the same thing ("no elevation data"), and must not poison
    # the profile with NaN either
    v_nan, _ = speed_profile(line, 1.5, 9.81, 14.0, 9.0,
                             bank=np.full(len(line), np.nan))
    assert np.array_equal(v_none, v_nan)

    one_real_station = np.zeros(len(line))
    one_real_station[5] = np.radians(3.0)
    with pytest.raises(ValueError, match="turn_sign"):
        speed_profile(line, 1.5, 9.81, 14.0, 9.0, bank=one_real_station)


def test_telem_calibration_raises_corner_speed_cap():
    left, right, _ = _stadium_edges()
    base = solve_corners(left, right)  # defaults: mu = 1.5
    # telemetry showing more grip than the GT3 default
    telem = {
        "lat_g": np.array([2.2, -2.2, 0.4, np.nan]),
        "long_g": np.array([1.8, -0.9, 0.0]),   # LMU sign: + braking, - accelerating
        "speed": np.array([200.0, 100.0, 150.0]),
    }
    hot = solve_corners(left, right, telem=telem)

    assert min(c["min_speed_kmh"] for c in hot) > min(c["min_speed_kmh"] for c in base)


def test_telem_calibration_reads_lmu_long_g_sign():
    # LMU's long_g is POSITIVE under braking (mLocalAccel.z; #70). The peaks below are
    # a real stored lap's (7045.npz: 99th pct braking +2.38 g, 1st pct at full throttle
    # -0.90 g). They are deliberately ASYMMETRIC: read the wrong way round the car
    # brakes at 0.90 g and accelerates at 2.38 g, which is what the old reading did,
    # and a symmetric pair could not tell the two readings apart.
    from simcoach.raceline import CarParams, _calibrate

    # lat_g's larger magnitude is on the NEGATIVE (right-hand) side, so a calibration
    # that dropped np.abs() would read mu 1.7 instead of 1.8.
    car = _calibrate(CarParams(), {"lat_g": np.array([1.7, -1.8]),
                                   "long_g": np.array([2.38, 0.4, -0.90, np.nan])})
    assert abs(car.mu - 1.8) < 1e-9
    assert abs(car.a_brake - 2.38 * car.g) < 1e-9
    assert abs(car.a_accel - 0.90 * car.g) < 1e-9
    assert car.a_brake > car.a_accel   # a car brakes harder than it accelerates
