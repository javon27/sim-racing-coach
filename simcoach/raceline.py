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


def speed_profile(line, mu, g, a_brake, a_accel, bank=None):
    """Friction-circle point-mass speed profile over a closed line.

    Cornering cap v_cap = sqrt(g*r*(mu + tanθ)/(1 - mu*tanθ)) where r = 1/kappa
    and θ is the per-station bank angle (radians); flat (bank None/zeros) reduces
    to sqrt(mu*g*r). Then a backward pass limiting braking to a_brake and a
    forward pass limiting accel to a_accel. Two loops around so the limits
    propagate across the start/finish seam.

    Returns (v, curvature), both shape (N,). v in m/s, curvature in 1/m.
    # ponytail: simple longitudinal caps, not a true traction-budget friction
    # circle. Upgrade to a coupled lat/long budget if the profile reads too hot
    # mid-corner.
    """
    line = np.asarray(line, dtype=float)
    n = len(line)
    kappa = _curvature(line)
    seg = _seg_lengths(line)                         # seg[i] = dist from i to i+1
    r = 1.0 / np.maximum(kappa, _EPS)
    tan_t = np.zeros(n) if bank is None else np.tan(np.asarray(bank, dtype=float))
    # banked friction-circle cap; clamp denominator when mu*tanθ -> 1 (grip
    # dominated by banking, cap would blow up) to a large but finite value.
    denom = np.maximum(1.0 - mu * tan_t, 1e-3)
    v = np.sqrt(g * r * (mu + tan_t) / denom)        # cornering cap

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


def _smooth_closed(x, win):
    """Closed-loop boxcar moving average (wrap the seam). win<=1 -> unchanged."""
    if win <= 1:
        return x
    k = np.ones(win) / win
    pad = np.concatenate([x[-win:], x, x[:win]])
    return np.convolve(pad, k, "same")[win:-win]


def _split_run_apexes(kv, min_prom):
    """Apex offsets within one curvature run (1D values kv). Local maxima whose
    separating valley is shallower than min_prom are MERGED (one corner); a valley
    with real prominence SPLITS (e.g. a chicane's two apexes). A flat plateau (a
    long constant-radius sweeper) has no prominent valley -> a single centered apex,
    so it isn't chopped into several corners."""
    m = len(kv)
    if m == 1:
        return [0]
    maxima = [i for i in range(m)
              if kv[i] >= kv[i - 1 if i > 0 else 0] and kv[i] >= kv[i + 1 if i < m - 1 else m - 1]]
    if not maxima:
        return [int(np.argmax(kv))]
    groups = [[maxima[0]]]
    for mi in maxima[1:]:
        prev = groups[-1][-1]
        valley = float(np.min(kv[prev:mi + 1]))
        if min(kv[prev], kv[mi]) - valley >= min_prom:
            groups.append([mi])                       # prominent valley -> new corner
        else:
            groups[-1].append(mi)                     # same corner
    out = []
    for g in groups:
        peak = max(kv[i] for i in g)
        flat = [i for i in g if kv[i] >= peak - 1e-9]  # center a plateau's apex
        out.append(flat[len(flat) // 2])
    return out


def _detect_apexes(kappa, kappa_min, min_prom):
    """Apex stations: contiguous runs of kappa >= kappa_min (closed loop), each
    split into corners by curvature prominence. Detects a corner wherever the track
    bends -- fast corners included -- while keeping a sustained arc as one."""
    n = len(kappa)
    apexes: list[int] = []
    for start, end in _runs(kappa >= kappa_min):
        seg = [start] if start == end else \
            [i % n for i in range(start, (end if end >= start else end + n) + 1)]
        kv = np.array([kappa[i] for i in seg])
        for off in _split_run_apexes(kv, min_prom):
            apexes.append(seg[off])
    return sorted(set(apexes))


def _corner_window(kappa, apex, n, max_half):
    """Entry/exit stations: walk out from the apex to the flanking curvature valleys
    (while curvature is non-increasing), bounded to max_half stations each side.
    Guarantees at least a +-1 window so the corner is never zero-length."""
    si = apex
    for _ in range(max_half):
        p = (si - 1) % n
        if kappa[p] <= kappa[si] and p != apex:
            si = p
        else:
            break
    ei = apex
    for _ in range(max_half):
        q = (ei + 1) % n
        if kappa[q] <= kappa[ei] and q != apex:
            ei = q
        else:
            break
    if si == apex:
        si = (apex - 1) % n
    if ei == apex:
        ei = (apex + 1) % n
    return si, ei


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


def solve_corners(left_edge, right_edge, car=CarParams(), telem=None, bank=None,
                  kappa_min=0.002, min_prom=0.0008, smooth_win=7):
    """Detect a first-pass corner set from the reconstructed corridor.

    Apexes are CURVATURE maxima of the centerline (where the track bends), not
    speed minima -- so fast corners (small speed drop) are found and adjacent
    corners a single speed threshold would merge stay separate, while a sustained
    constant-radius arc stays one corner (prominence split, not fixed spacing). A
    speed profile over the min-curvature line still supplies each corner's
    ``min_speed`` (calibrated from ``telem`` peak lat_g/long_g when given).

    Tunables (calibrated against driver-verified maps -- Algarve/Imola/Monza,
    avg F1 ~0.91): ``kappa_min`` 1/m curvature floor (~1/500 m radius), ``min_prom``
    curvature prominence to split adjacent corners, ``smooth_win`` boxcar stations.

    Each corner: ``{apex_idx, start_idx, end_idx, apex_dist, start_dist, end_dist,
    min_speed}``. The *_idx values are station indices aligned with the input
    edges / ribbon dist arrays -- use these for placement. start>end is a valid
    S/F-seam wrap; the window is never zero-length.
    """
    if telem is not None:
        car = _calibrate(car, telem)

    left = np.asarray(left_edge, dtype=float)
    right = np.asarray(right_edge, dtype=float)
    n = len(left)
    if n < 3:
        return []
    center = 0.5 * (left + right)

    # Speed profile on the min-curvature line -> per-corner min_speed (bank-aware).
    _, line = min_curvature_line(left, right)
    v, _ = speed_profile(line, car.mu, car.g, car.a_brake, car.a_accel, bank=bank)

    # Apex detection from centerline curvature.
    kappa = _smooth_closed(_curvature(center), smooth_win)
    seg = _seg_lengths(center)
    dist = _dist_along(center)
    length = float(dist[-1] + seg[-1])                # closed-loop total
    apexes = _detect_apexes(kappa, kappa_min, min_prom)
    max_half = max(1, int(round(80.0 / max(length, _EPS) * n)))  # window bound ~80 m/side

    corners = []
    for i in apexes:
        si, ei = _corner_window(kappa, i, n, max_half)
        corners.append({
            "apex_idx": int(i),
            "start_idx": int(si),
            "end_idx": int(ei),
            "apex_dist": float(dist[i]),
            "start_dist": float(dist[si]),
            "end_dist": float(dist[ei]),
            "min_speed": float(v[i]),
        })

    corners.sort(key=lambda c: c["apex_dist"])
    return corners
