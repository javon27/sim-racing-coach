"""Naive optimal-line solver over a track corridor (the two reconstructed edges).

Quick first pass, NOT a lap-time optimizer: an elastic-band (curve-shortening)
minimum-curvature line, a friction-circle point-mass speed profile, and corner
detection from the resulting speed trace. Pure numpy, closed-loop, no scipy/QP.

The track is a corridor of N stations; ``left_edge[i]``/``right_edge[i]`` are the
opposite boundary points at station i. The line picks a lateral fraction
``alpha[i]`` in [0,1] per station (0 = left edge, 1 = right edge).

Car knobs (mu, a_brake, a_accel) are physical and vary by car -- GT3 defaults,
tunable, and calibratable from captured telemetry (peak lat_g -> mu, peak
long_g -> accel/brake). See the boundary-laps plan.
"""

from dataclasses import dataclass, replace

import numpy as np

_EPS = 1e-9


@dataclass
class CarParams:
    """Physical point-mass knobs. GT3-ish defaults; tune or calibrate per car."""
    mu: float = 1.5              # peak friction coefficient (lateral, ~= peak lat_g)
    g: float = 9.81             # m/s^2
    a_brake: float = 14.0       # max braking decel, m/s^2 (~1.4 g)
    a_accel: float = 9.0        # max forward accel, m/s^2 (~0.9 g)


def _curvature(line):
    """Discrete (Menger) curvature per station on a closed loop, 1/m. Shape (N,)."""
    p_prev = np.roll(line, 1, axis=0)
    p_next = np.roll(line, -1, axis=0)
    a = np.linalg.norm(p_prev - line, axis=1)
    b = np.linalg.norm(line - p_next, axis=1)
    c = np.linalg.norm(p_prev - p_next, axis=1)
    v1 = line - p_prev
    v2 = p_next - line
    cross2 = np.abs(v1[:, 0] * v2[:, 1] - v1[:, 1] * v2[:, 0])  # = 2 * triangle area
    denom = a * b * c
    return np.where(denom > _EPS, 2.0 * cross2 / denom, 0.0)  # Menger: 4A/(abc)


def _seg_lengths(line):
    """Distance from station i to i+1 (closed loop). Shape (N,)."""
    return np.linalg.norm(np.roll(line, -1, axis=0) - line, axis=1)


def _dist_along(line):
    """Cumulative distance along the line, 0 at station 0. Shape (N,)."""
    seg = _seg_lengths(line)
    return np.concatenate([[0.0], np.cumsum(seg)[:-1]])


def min_curvature_line(left_edge, right_edge, iterations=300, relax=0.2):
    """Minimum-curvature racing line by elastic-band relaxation.

    Starts at the centerline (alpha=0.5) and repeatedly nudges each station
    toward the midpoint of its neighbours (Laplacian / curve-shortening flow),
    projected onto the station's left->right axis and clamped to [0,1] so the
    line stays inside the corridor. Closed loop (indices wrap).

    Returns (alpha, line): alpha shape (N,) in [0,1]; line shape (N,2).
    """
    left = np.asarray(left_edge, dtype=float)
    right = np.asarray(right_edge, dtype=float)
    lateral = right - left                          # (N,2) across-track axis per station
    denom = np.sum(lateral * lateral, axis=1)       # |lateral|^2
    alpha = np.full(len(left), 0.5)

    for _ in range(iterations):
        line = left + alpha[:, None] * lateral
        target = 0.5 * (np.roll(line, 1, axis=0) + np.roll(line, -1, axis=0))
        delta = target - line                       # world-space straightening move
        d_alpha = np.where(denom > _EPS,
                           np.sum(delta * lateral, axis=1) / denom, 0.0)
        alpha = np.clip(alpha + relax * d_alpha, 0.0, 1.0)

    return alpha, left + alpha[:, None] * lateral


def speed_profile(line, mu, g, a_brake, a_accel):
    """Friction-circle point-mass speed profile over a closed line.

    Cornering cap v_cap = sqrt(mu*g / kappa), then a backward pass limiting
    braking to a_brake and a forward pass limiting accel to a_accel. Two loops
    around so the limits propagate across the start/finish seam.

    Returns (v, curvature), both shape (N,). v in m/s, curvature in 1/m.
    # ponytail: simple longitudinal caps, not a true traction-budget friction
    # circle. Upgrade to a coupled lat/long budget if the profile reads too hot
    # mid-corner.
    """
    line = np.asarray(line, dtype=float)
    n = len(line)
    kappa = _curvature(line)
    seg = _seg_lengths(line)                         # seg[i] = dist from i to i+1
    v = np.sqrt(mu * g / np.maximum(kappa, _EPS))    # cornering cap

    for _ in range(2):
        for i in range(n - 1, -1, -1):               # backward: brake into i from i+1
            ds = seg[i]
            v[i] = min(v[i], np.sqrt(v[(i + 1) % n] ** 2 + 2.0 * a_brake * ds))
        for i in range(n):                           # forward: accel into i from i-1
            ds = seg[(i - 1) % n]
            v[i] = min(v[i], np.sqrt(v[(i - 1) % n] ** 2 + 2.0 * a_accel * ds))

    return v, kappa


def _runs(mask):
    """Contiguous runs of True in a closed-loop mask, as (start, end) index pairs.

    Handles a run that straddles the start/finish seam by rotating it off index 0.
    """
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    n = len(mask)
    shift = np.flatnonzero(~mask)[0] if (idx.size < n and mask[0] and mask[-1]) else 0
    rolled = np.roll(mask, -shift)
    runs = []
    i = 0
    while i < n:
        if rolled[i]:
            j = i
            while j + 1 < n and rolled[j + 1]:
                j += 1
            runs.append(((i + shift) % n, (j + shift) % n))
            i = j + 1
        else:
            i += 1
    return runs


def _calibrate(car, telem):
    """Calibrate mu/a_brake/a_accel from observed telemetry peaks (g-unit channels)."""
    lat = np.abs(np.asarray(telem.get("lat_g", []), dtype=float))
    lon = np.asarray(telem.get("long_g", []), dtype=float)
    lat = lat[np.isfinite(lat)]
    lon = lon[np.isfinite(lon)]
    mu = float(lat.max()) if lat.size and lat.max() > 0 else car.mu
    a_accel = car.a_accel
    a_brake = car.a_brake
    if lon.size:
        if lon.max() > 0:
            a_accel = float(lon.max()) * car.g       # peak +long_g (g) -> m/s^2
        if lon.min() < 0:
            a_brake = float(-lon.min()) * car.g      # peak -long_g (g) -> m/s^2
    return replace(car, mu=mu, a_brake=a_brake, a_accel=a_accel)


def solve_corners(left_edge, right_edge, car=CarParams(), telem=None):
    """Solve the line + speed profile and return a first-pass corner set.

    Optionally calibrates ``car`` from ``telem`` (a channels dict with lat_g /
    long_g / speed) before solving; falls back to CarParams defaults when None.

    Corners are the speed minima of the solved line. For each returns
    ``{apex_dist, start_dist, end_dist, min_speed}`` with distances measured
    ALONG the solved line (metres from station 0); min_speed in m/s. start/end
    are the flanking speed peaks (cruise before braking / after the corner).

    # ponytail: a corner straddling the start/finish seam can report
    # start_dist > end_dist. Fine for v0.1 (corners live away from the seam);
    # normalise against total line length if a consumer needs monotonic windows.
    """
    if telem is not None:
        car = _calibrate(car, telem)

    alpha, line = min_curvature_line(left_edge, right_edge)
    v, kappa = speed_profile(line, car.mu, car.g, car.a_brake, car.a_accel)
    dist = _dist_along(line)

    # A corner = a contiguous region where speed drops well below cruise. The
    # threshold (halfway between the slowest and fastest point) is robust to the
    # discretization ripple in v that trips up raw local-minima detection.
    v_lo, v_hi = float(v.min()), float(v.max())
    if v_hi - v_lo < 1e-6:                        # constant speed -> no distinct corner
        return []
    threshold = v_lo + 0.5 * (v_hi - v_lo)

    corners = []
    for start, end in _runs(v < threshold):
        # apex = slowest station in the run (walk the run, wrapping the seam)
        n = len(line)
        idx = [start] if start == end else \
            [i % n for i in range(start, (end if end >= start else end + n) + 1)]
        apex = min(idx, key=lambda i: v[i])
        corners.append({
            "apex_dist": float(dist[apex]),
            "start_dist": float(dist[start]),
            "end_dist": float(dist[end]),
            "min_speed": float(v[apex]),
        })

    corners.sort(key=lambda c: c["apex_dist"])
    return corners
